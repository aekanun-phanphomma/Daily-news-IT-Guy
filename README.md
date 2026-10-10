# Daily DevOps & Infra News Bot

A Discord bot that posts one digest of DevOps, cloud, Kubernetes and security
news every morning at **07:00 Bangkok time**. No server, no database, no
container, and no bill — it runs entirely on GitHub Actions' free tier.

```text
40 sources: RSS + GitHub release feeds + Hacker News & GitHub Search APIs
        │
        ▼
GitHub Actions (cron: 0 0 * * *  ==  07:00 ICT)
        │
        ├─ 1. fetch every source (failures are logged and skipped)
        ├─ 2. drop anything older than 48h
        ├─ 3. drop anything already in data/seen_urls.json
        ├─ 4. round-robin to a balanced pool of ≤60 candidates
        ├─ 5. AI triage: score 0-10, label ACTION / WATCH / FYI, keep 14
        ├─ 6. AI summary: what changed + what the team should do
        └─ 7. POST to Discord, grouped by priority
        │
        ▼
Discord  (+ LinkedIn and Facebook Page, both optional)
        │
        ▼
commit the updated data/seen_urls.json back to main
```

The point of steps 4–6 is that this is a triage tool, not a feed reader.
Recency cannot tell a Kubernetes deprecation that breaks your cluster in 90
days from a conference announcement posted the same hour; the ranker can.

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
| `DISCORD_FORMAT` | no | `text` (default) or `embed` — see below |
| `DRY_RUN` | no | `1` to preview without posting or saving state |
| `GEMINI_API_KEY` | no | Enables AI triage **and** Thai summaries |
| `GEMINI_MODEL` | no | Comma-separated model list, tried in order |
| `LINKEDIN_ACCESS_TOKEN` | no | Also post to LinkedIn — read the caveat below |
| `LINKEDIN_AUTHOR_URN` | no | Skips the `/v2/userinfo` lookup |
| `LINKEDIN_VERSION` | no | `YYYYMM`, default `202601` |
| `FACEBOOK_PAGE_ID` | no | Also post to a Facebook Page — set both or neither |
| `FACEBOOK_PAGE_TOKEN` | no | Page access token, non-expiring — see below |
| `FACEBOOK_API_VERSION` | no | Graph API version, default `v26.0` |
| `GITHUB_TOKEN` | no | Raises the GitHub Search rate limit; Actions supplies it |

Without `GEMINI_API_KEY` the bot still works: items are picked by the
round-robin instead of the ranker, grouped by category instead of priority,
and each carries the feed's own English blurb instead of a Thai summary.

---

## Sources

| Category | Sources |
| --- | --- |
| ☁️ Cloud & Infrastructure | AWS What's New, AWS Architecture, AWS Service Health, Azure Updates, Azure Tech Community, Google Cloud, Cloudflare |
| 🚀 Platform & Kubernetes | Kubernetes Blog, CNCF, Istio, The New Stack, Kong, and release feeds for AKS, Kubernetes, Istio, Headlamp, OPA |
| 🛠️ DevOps & IaC | HashiCorp, Docker, GitHub, GitLab, AWS DevOps, Azure DevOps |
| 💾 Data & Streaming | Redis, Confluent (Kafka), Elastic, PostgreSQL, and release feeds for Kafka, Redis, Elasticsearch |
| 📊 Observability | Grafana, Prometheus, OpenTelemetry, Datadog, New Relic, and release feeds for Fluentd, Fluent Bit, Grafana, Prometheus |
| 🤖 AI & Models | OpenAI, Hugging Face, Google DeepMind |
| 🔒 Security | The Hacker News, Krebs on Security |
| 🔥 Trending | Hacker News, GitHub (new repos by stars) |

To add a source, append one dict to `FEEDS` in [src/main.py](src/main.py) with
a `name`, a `category` key from `CATEGORIES`, and the feed `url`. Nothing else
needs to change.

**Why so many GitHub release feeds.** Deprecations, breaking changes and
version-support windows get announced in release notes, not in blog posts. A
vendor blog is marketing; a release note is the contract. Their titles are
thin (`Release 2026-09-04`) but the body carries the detail, and the body is
what gets summarized.

### Feeds that look fine and are not

Every URL here was probed before being added, because **a 200 response is not
a working feed.** The bot treats "parsed, but zero entries" as a failure for
exactly this reason.

| Obvious URL | What it actually does | What works |
| --- | --- | --- |
| `azure.microsoft.com/en-us/updates/feed/` | HTML error page, HTTP 200 | `microsoft.com/releasecommunications/api/v2/azure/rss` |
| `cloud.google.com/blog/feed` | HTML, 0 entries, HTTP 200 | `cloudblog.withgoogle.com/rss/` |
| `confluent.io/blog/feed/` | 404 | `confluent.io/feed/` |
| `kafka.apache.org/blog.rss` | 404 | GitHub releases feed |
| `konghq.com/blog/feed` | 404 | `konghq.com/feed` |
| Azure Tech Community per-board RSS | 404 since the platform migration | the Azure category feed |

Apigee and GKE release notes were probed and rejected for a different reason:
every entry is titled with a bare date, which is useless as a headline.

---

## Output format

`DISCORD_FORMAT` picks between two renderings, and the default is the boring
one on purpose.

| | `text` (default) | `embed` |
| --- | --- | --- |
| Needs `Embed Links` on the channel | no | **yes** |
| Characters per message | 2,000 | 4,096 |
| Messages per digest | 2–3 | 1 |
| Coloured sidebar, footer, timestamp | no | yes |

A Discord webhook has no role of its own — it inherits `@everyone`'s
permissions in the target channel. When that role is missing **Embed Links**,
Discord accepts the POST, returns success, *keeps the embed on the message
object*, and renders nothing. The API genuinely reports `1 embed(s) stored`
while the channel shows a blank message. There is no response field that tells
you this happened.

That is a bad property for a bot nobody watches, so the default format is the
one with no permission dependency.

To switch to embeds: grant **Edit Channel → Permissions → @everyone → Embed
Links**, then set `DISCORD_FORMAT: embed` in
[.github/workflows/daily-news.yml](.github/workflows/daily-news.yml).

Both formats share the delivered-items invariant described below. In `text`
mode each message carries its own item list, so if part 2 of 3 fails, part 1's
items are recorded as sent, parts 2 and 3 are not, and the job still exits
non-zero.

---

## LinkedIn (optional secondary channel)

Set `LINKEDIN_ACCESS_TOKEN` and the digest is also posted to your LinkedIn
feed. Leave it unset and nothing changes.

### The catch you need to know before you start

**The token expires 60 days after it is issued, and you cannot automate the
renewal.** Standard apps using `w_member_social` are not issued a refresh
token at all — programmatic refresh is reserved for approved Marketing
Developer Platform partners. Every 60 days you re-run the OAuth flow by hand
and update the secret. Put a calendar reminder at day 55.

This is why a LinkedIn failure **fails the whole job** even though Discord
already went out: a 401 here needs a human, and a bot that goes quiet about it
is worse than a red X. It does not, however, stop the other secondary
channels — `main()` attempts all of them and only then fails.

### Setup

1. Create an app at <https://www.linkedin.com/developers/apps>, associate it
   with a LinkedIn Page you admin, and add the **Share on LinkedIn** and
   **Sign In with LinkedIn using OpenID Connect** products.
2. Run the OAuth authorization-code flow with scopes `openid profile
   w_member_social` and exchange the code for an access token.
3. `gh secret set LINKEDIN_ACCESS_TOKEN --body "..."`

`LINKEDIN_AUTHOR_URN` is optional — without it the bot calls `/v2/userinfo`
and derives `urn:li:person:{sub}` itself. Set it to skip that request.

`LINKEDIN_VERSION` defaults to `202601`. LinkedIn retires API versions on a
rolling schedule and a stale one answers **426**, not 200; the error message
says so explicitly and the fix is a workflow edit.

### What posting to LinkedIn actually requires in code

It is not Discord with a different URL, which is why `render_linkedin()` is a
separate renderer rather than a parameter:

* **No markdown.** `**bold**` renders as literal asterisks and
  `[title](url)` as literal brackets. Structure has to come from line breaks,
  and URLs go in bare for LinkedIn to auto-link.
* **Reserved characters must be backslash-escaped** — `| { } @ [ ] ( ) < > #
  \ * _ ~` — every one of them, even as ordinary punctuation, because the
  Posts API parses `commentary` for inline entities. An unescaped `(` is a
  422, not a cosmetic bug. See `escape_linkedin()`.
* **3,000 characters**, against Discord's 2,000 per message across several
  messages. One post, so the tail gets dropped if the digest runs long.

---

## Facebook Page (optional secondary channel)

Set `FACEBOOK_PAGE_ID` and `FACEBOOK_PAGE_TOKEN` and the digest is also posted
to your Page, in Thai. Leave both unset and nothing changes. Setting only one
is a hard error rather than a silent skip — a half-finished setup that looks
successful in the logs is the worst of both.

**Personal profiles are not possible.** Meta removed `publish_actions` in 2018
and shipped no replacement. The Share dialog, where a human clicks post, is the
only path to a profile. A Page is the only automatable target.

### The thing that makes this easier than LinkedIn

**The Page token does not expire.** No 60-day rotation, no calendar reminder.
`GET /debug_token` on it reads `Expires: Never`.

That property is not free, though — it holds only while two things stay true:

* **The Meta app stays in Development mode.** Publishing it puts every token
  back on a 60-day clock and pulls Business Verification into scope for
  permissions we do not need. There is no reason to publish: Development mode
  already allows the app's own admins to act on Pages they administer, and you
  are the only user this app will ever have.
* **The Facebook password does not change.** That revokes every token the
  account ever issued, this one included.

### Getting the token

1. Create an app at <https://developers.facebook.com/apps> with the use case
   **Manage everything on your Page**. Confirm `pages_manage_posts`,
   `pages_read_engagement` and `pages_show_list` all read **Ready for testing**
   under that use case's permissions tab.
2. Open the [Graph API Explorer][gae], select the app, tick those three
   permissions, and click **Generate Access Token**.
3. **On the consent screen, tick the Page.** Skipping this is the one mistake
   that produces no error at all: the token is issued, it is valid, it simply
   cannot see any Page, and step 4 returns an empty list with a 200.
4. Query `me/accounts?fields=id,name,access_token`. The `id` is
   `FACEBOOK_PAGE_ID`, the `access_token` is `FACEBOOK_PAGE_TOKEN`.
5. Paste the Page token into the [Access Token Debugger][atd] and confirm two
   lines: **Type: Page** (not User) and **Expires: Never**. Do not skip this —
   if the user token behind step 4 was short-lived, everything works today and
   dies silently in sixty days.
6. `gh secret set FACEBOOK_PAGE_ID` and `gh secret set FACEBOOK_PAGE_TOKEN`.

[gae]: https://developers.facebook.com/tools/explorer/
[atd]: https://developers.facebook.com/tools/debug/accesstoken/

Permissions showing **Ready for testing** (Standard Access) is the finished
state, not an intermediate one. Advanced Access exists so that *other people*
can connect *their* Pages to your app, which is not this.

### What posting to Facebook actually requires in code

`render_facebook()` is the third renderer, and it is the shortest — the
constraints run opposite to LinkedIn's at almost every point:

* **No escaping at all.** Facebook has no markdown, so `**bold**` would post
  as literal asterisks, but it also has no inline-entity parser, so there is
  no `escape_facebook()` to match `escape_linkedin()`. Plain text goes out
  exactly as built.
* **63,206 characters** against LinkedIn's 3,000. The digest lands near 3k, so
  there is no budget arithmetic and no dropped tail. The limit appears in the
  code only as a guard rail.
* **Thai, from `summary`.** Same audience as Discord, so the existing Thai
  pass covers it and no extra Gemini call is added. `summary_en` stays
  LinkedIn's.
* **Emoji stay in**, unlike LinkedIn. That was a register choice for a
  CV-adjacent feed, not a technical limit.
* **The `link` field is deliberately unset.** It accepts exactly one URL and
  renders a preview card for it, which would promote one item above the other
  thirteen. URLs go in the body bare and Facebook auto-links them.

The token goes in the POST form body rather than the query string, so it stays
out of proxy logs and out of any error that echoes the request line.

### Why both secondary channels now run before the job fails

Adding a second channel turned the old `return 1` on a LinkedIn failure into a
bug. LinkedIn's token expiry is not a possibility but a certainty every sixty
days, and on that morning it would have skipped the Facebook post entirely for
a reason Facebook had nothing to do with. `main()` now attempts every channel,
collects the failures, and fails the job at the end. Dedup state is still
saved before any of it runs, so neither channel can cost a day of Discord news.

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
`gemini-2.5-flash` on the free tier, overridable with `GEMINI_MODEL`.

Google retires model ids on a schedule — `gemini-2.0-flash` was already gone
when this was first wired up. A dead id makes the summaries stop without the
digest failing, so it shows up as a `WARNING` in the job log rather than a red
X. The API's 404 names its own replacement, and `GEMINI_MODEL` means acting on
that is a workflow edit, not a code change.

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
