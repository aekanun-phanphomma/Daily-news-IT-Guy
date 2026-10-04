# Daily DevOps & Infra News Bot

A Discord bot that posts one digest of DevOps, cloud, Kubernetes and security
news every morning at **07:00 Bangkok time**. No server, no database, no
container, and no bill — it runs entirely on GitHub Actions' free tier.

```text
13 RSS feeds + Hacker News API + GitHub Search API
        │
        ▼
GitHub Actions (cron: 0 0 * * *  ==  07:00 ICT)
        │
        ├─ 1. fetch every source (failures are logged and skipped)
        ├─ 2. drop anything older than 48h
        ├─ 3. drop anything already in data/seen_urls.json
        ├─ 4. pick ≤20 items, round-robin across categories
        ├─ 5. (optional) one-line Thai summaries via Gemini
        └─ 6. POST one embed to the Discord webhook
        │
        ▼
#daily-devops-news
        │
        ▼
commit the updated data/seen_urls.json back to main
```

---

## Setup

Four steps. Only the first one needs a browser.

### 1. Create the Discord webhook

In Discord: **Server Settings → Integrations → Webhooks → New Webhook**, point
it at the channel you want, then **Copy Webhook URL**. It looks like
`https://discord.com/api/webhooks/<id>/<token>`.

That URL *is* the credential — anyone holding it can post to the channel. It
goes in a GitHub secret, never in a commit.

### 2. Store it as a repository secret

```bash
gh secret set DISCORD_WEBHOOK_URL --body "https://discord.com/api/webhooks/..."
```

Or: **Settings → Secrets and variables → Actions → New repository secret**.

### 3. Test it

Actions tab → **Daily DevOps News** → **Run workflow**.

Tick **dry run** to print the digest into the job log without posting anything
and without touching `seen_urls.json` — useful for checking the formatting.
Leave it unticked to send a real message.

### 4. Nothing

The cron is already active. It fires at 00:00 UTC daily.

---

## Local development

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

DRY_RUN=1 python src/main.py                        # preview, sends nothing
```

`DRY_RUN=1` is the safe mode: it fetches and formats everything, prints the
embed, and leaves `data/seen_urls.json` untouched. To send for real, set
`DISCORD_WEBHOOK_URL` in your shell instead.

### Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | yes | Where the digest is posted |
| `DRY_RUN` | no | `1` to preview without posting or saving state |
| `GEMINI_API_KEY` | no | Enables the Thai one-line summaries (Phase 2) |
| `GITHUB_TOKEN` | no | Raises the GitHub Search rate limit; Actions supplies it |

---

## Sources

| Category | Sources |
| --- | --- |
| ☁️ Cloud & Infrastructure | AWS What's New, Azure Updates, Google Cloud Blog |
| 🚀 Platform & Kubernetes | Kubernetes Blog, CNCF Blog, Istio Blog |
| 🛠️ DevOps & IaC | HashiCorp Blog, Docker Blog, GitHub Blog |
| 🔒 Security | The Hacker News, Krebs on Security |
| 🔥 Trending | Hacker News (top 5), GitHub (new repos by stars) |

To add a source, append one dict to `FEEDS` in [src/main.py](src/main.py) with
a `name`, a `category` key from `CATEGORIES`, and the feed `url`. Nothing else
needs to change.

Two of the URLs in the obvious places are dead and the working ones are not
guessable, so they are worth noting:

* `azure.microsoft.com/en-us/updates/feed/` returns an **HTML error page with
  HTTP 200**. The live feed is `microsoft.com/releasecommunications/api/v2/azure/rss`.
* `cloud.google.com/blog/feed` also answers 200 with HTML. The real one is
  `cloudblog.withgoogle.com/rss/`.

A 200 response is not a working feed. The bot treats "parsed, but zero
entries" as a failure for exactly this reason.

---

## Why it is built this way

**Why GitHub Actions instead of a cron box or a Lambda?**
The job is one HTTP burst a day. Actions gives 2,000 free minutes a month on
private repos and *unlimited* minutes on public ones, already has the repo
checked out, already has a secret store, and keeps the run logs. A VM for this
would cost money and need patching.

**Why a JSON file in git instead of a database?**
State here is a few hundred 16-character strings. Git already gives durability,
history, free hosting and atomic writes; a database would add a service to
operate for no benefit. The commit *is* the write transaction — and because the
commit only happens after Discord confirms delivery, the state can never claim
to have sent something that was never sent.

**Why hash the URLs instead of storing them?**
Privacy is not the point — size is. 2,000 full URLs is a ~200 KB file rewritten
daily, which bloats the git history forever. 2,000 truncated hashes is ~40 KB.
Truncating SHA-256 to 16 hex characters leaves 64 bits; at 2,000 stored values
the chance of a collision is around 1 in 10¹⁴.

**Why normalize URLs before hashing?**
The same AWS announcement arrives through different feeds with different `utm_*`
parameters. Without stripping those, one article counts as several. See
`normalize_url()`.

**Why does `build_embed()` return the list of items it rendered?**
Because Discord caps an embed description at 4,096 characters, and anything cut
by that cap was *not delivered*. Only the items that actually made it into the
message are written to `seen_urls.json`; the rest stay unseen and get another
chance tomorrow. This is the one piece of logic in the repo that is easy to get
wrong in a way nobody notices for weeks.

**Why round-robin across categories instead of taking the newest 20?**
AWS What's New publishes dozens of entries a day and would fill the whole
digest on its own. `pick_for_digest()` takes the newest unused item from each
category in turn, so Security and Kubernetes keep a slot.

**Why fetch with `requests` and only then hand the bytes to feedparser?**
feedparser can fetch a URL itself, but then it uses its own HTTP stack — no
shared User-Agent, no timeout, no retry. Azure's CDN answers feedparser's
default User-Agent with a 403. One session with a browser-ish UA, a 15-second
timeout and a retry on 5xx/429 fixes all of it at once.

**Why does the bot stay silent when there is nothing new?**
A "no news today" message every morning teaches people to ignore the channel.
Silence plus a clear log line is the better failure mode. The run still exits 0,
because having already sent everything is a success, not an error.

**Why one batched Gemini call instead of one per item?**
The free tier allows ~15 requests per minute. Twenty per-item calls would need
~80 seconds of sleeping to stay inside that — most of the time budget for the
whole job. One request with all 20 titles costs about a second. The tradeoff is
parsing numbered output, and anything that comes back unparseable is just left
without a summary.

**Why is `GITHUB_TOKEN` optional?**
Because the trending lookup makes exactly one search request, and the
unauthenticated limit is 10 per minute. The token is passed in CI purely
because it is free to do so.

---

## Operational notes

* **The schedule is approximate.** GitHub's scheduler is best-effort and
  on-the-hour queues are long, so the run often lands 5–30 minutes late. Fine
  for a date-stamped digest; do not build anything minute-critical on it.
* **Scheduled workflows get disabled after 60 days of repo inactivity.** This
  bot commits to the repo on most days, which counts as activity and keeps
  itself alive. If you ever make the dedup state non-committing, add a keepalive
  or expect it to stop silently after two months.
* **A failed send is a deliberate hard failure.** `send_discord()` raises, the
  step fails, the commit step is skipped, and the items stay unseen. You get a
  red X in the Actions tab and tomorrow's run picks up today's news.
* **Running twice in a row sends nothing the second time.** That is the dedup
  working. To re-test for real, empty the `hashes` array in
  `data/seen_urls.json`.
* **The commit is tagged `[skip ci]`.** This workflow only triggers on
  `schedule` and `workflow_dispatch`, so it cannot loop on its own push, but
  the tag protects any push-triggered workflow added later.

---

## Phase 2 — AI summaries (optional)

Already implemented in `add_summaries()`; it is dormant until the key exists.

```bash
gh secret set GEMINI_API_KEY --body "..."     # aistudio.google.com/apikey
```

Each item then gets a one-line Thai summary underneath its link. Model:
`gemini-2.0-flash` on the free tier.

**Lower `MAX_TOTAL_ITEMS` when you turn this on.** A 20-item digest without
summaries runs ~3,800 of the 4,096 available characters, so adding a line of
Thai per item will overflow and `build_embed()` will start cutting the tail.
Nothing is lost when that happens — the cut items are never marked seen, so
they arrive the next morning — but you get a lagging backlog rather than a
same-day digest. `MAX_TOTAL_ITEMS = 10` is about right with summaries on.

The code uses the **`google-genai`** SDK, not the older
`google-generativeai` package, which is deprecated and no longer gets fixes.

> Note on Claude: a Claude Pro subscription does not include API access — the
> Claude API is billed separately. Gemini's free tier is used here to keep the
> project at $0.

## Phase 3 — ideas, not plans

* Keyword scoring so AKS / EKS / Terraform / Istio / WAF items float to the top
* A second output: Slack, Teams, or LINE
* A GitHub Pages archive of past digests
* SQLite instead of JSON, once you want to query history rather than just dedup
* A weekly roll-up of the most-discussed stories

---

## Layout

```text
.github/workflows/daily-news.yml   cron, permissions, the commit-back step
src/main.py                        the entire bot (~400 lines, one file on purpose)
data/seen_urls.json                dedup state, rewritten by CI
requirements.txt                   pinned deps
```
