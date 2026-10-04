#!/usr/bin/env python3
"""
Daily DevOps & Infrastructure News Bot.

Fetches RSS feeds + a couple of JSON APIs, drops anything we have already
sent before, and posts what is left to Discord as a single grouped embed.

Design notes (this repo is meant to be read, not just run):

* There is no server and no database. State lives in ``data/seen_urls.json``,
  which GitHub Actions commits back to the repo after every successful run.
  Git is the database; a commit is the write transaction.
* The script is deliberately one file. The whole thing is ~400 lines and one
  person has to maintain it, so a package layout would cost more than it buys.
* Every network call is wrapped. One dead feed must never take down the digest,
  because the failure mode of a news bot is "silent for a week before anyone
  notices".
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

MAX_AGE_HOURS = 48          # ignore anything older than this
MAX_ITEMS_PER_FEED = 5      # stop one chatty feed from owning the digest
MAX_TOTAL_ITEMS = 20        # readable message length
MAX_SEEN_HASHES = 2000      # bound the dedup file so the repo stays small
MAX_TITLE_CHARS = 180       # one verbose headline should not own three lines

HTTP_TIMEOUT = 15           # seconds, per request
DISCORD_EMBED_DESC_LIMIT = 4096

# Bangkok is UTC+7 year round and has never observed DST, so a fixed offset is
# correct here. Using it avoids depending on the `tzdata` package, which is not
# bundled with CPython on Windows.
BANGKOK = timezone(timedelta(hours=7))

REPO_ROOT = Path(__file__).resolve().parent.parent
SEEN_PATH = REPO_ROOT / "data" / "seen_urls.json"

# Some CDNs (Azure's in particular) answer feedparser's default User-Agent with
# a 403, so every request goes out looking like a normal browser client.
USER_AGENT = (
    "Mozilla/5.0 (compatible; daily-devops-news/1.0; "
    "+https://github.com/aekanun-phanphomma/Daily-news-IT-Guy)"
)

# Display order of the digest. The key is also what each feed tags itself with.
CATEGORIES: dict[str, dict[str, str]] = {
    "cloud":    {"emoji": "\u2601\ufe0f", "label": "Cloud & Infrastructure"},
    "platform": {"emoji": "\U0001F680",   "label": "Platform & Kubernetes"},
    "devops":   {"emoji": "\U0001F6E0\ufe0f", "label": "DevOps & IaC"},
    "security": {"emoji": "\U0001F512",   "label": "Security"},
    "trending": {"emoji": "\U0001F525",   "label": "Trending"},
}

FEEDS: list[dict[str, str]] = [
    # Cloud & Infrastructure
    {"name": "AWS What's New", "category": "cloud",
     "url": "https://aws.amazon.com/about-aws/whats-new/recent/feed/"},
    {"name": "Azure Updates", "category": "cloud",
     # azure.microsoft.com/en-us/updates/feed/ now serves an HTML error page
     # with HTTP 200. This is the backend the Azure Updates site itself reads.
     "url": "https://www.microsoft.com/releasecommunications/api/v2/azure/rss"},
    {"name": "Google Cloud Blog", "category": "cloud",
     # cloud.google.com/blog/feed answers 200 with HTML, not RSS; this is the
     # real feed behind the same blog.
     "url": "https://cloudblog.withgoogle.com/rss/"},
    # Platform & Kubernetes
    {"name": "Kubernetes Blog", "category": "platform",
     "url": "https://kubernetes.io/feed.xml"},
    {"name": "CNCF Blog", "category": "platform",
     "url": "https://www.cncf.io/blog/feed/"},
    {"name": "Istio Blog", "category": "platform",
     "url": "https://istio.io/latest/blog/feed.xml"},
    # DevOps & IaC
    {"name": "HashiCorp Blog", "category": "devops",
     "url": "https://www.hashicorp.com/blog/feed.xml"},
    {"name": "Docker Blog", "category": "devops",
     "url": "https://www.docker.com/blog/feed/"},
    {"name": "GitHub Blog", "category": "devops",
     "url": "https://github.blog/feed/"},
    # Security
    {"name": "The Hacker News", "category": "security",
     "url": "https://feeds.feedburner.com/TheHackersNews"},
    {"name": "Krebs on Security", "category": "security",
     "url": "https://krebsonsecurity.com/feed/"},
]

# Query-string keys that only exist for analytics. Stripping them means the same
# article shared through two different feeds hashes to one value.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_reader", "utm_name", "mc_cid", "mc_eid", "fbclid", "gclid",
    "ref", "source",
}

log = logging.getLogger("news-bot")


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    category: str
    published: datetime
    summary: str | None = None

    @property
    def hash(self) -> str:
        return url_hash(self.link)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Canonical form of a URL, used as the dedup key.

    Lowercases the host, drops the fragment, and removes tracking parameters.
    Without this, the same AWS post arriving via two feeds counts as two items.
    """
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        path,
        urlencode(query),
        "",  # fragment
    ))


def url_hash(url: str) -> str:
    """First 16 hex chars of SHA-256 over the normalized URL.

    64 bits of hash. At our ceiling of 2,000 stored values the birthday
    collision probability is around 1e-14, so the truncation is free and the
    JSON file stays a quarter of the size.
    """
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:16]


def build_session() -> requests.Session:
    """A session that retries transient failures instead of losing a feed."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    retry = Retry(
        total=2,
        backoff_factor=1.0,            # 0s, 1s, 2s
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def parse_iso8601(value: str | None) -> datetime:
    """Parse a GitHub API timestamp, falling back to now on anything odd.

    GitHub sends `2026-10-04T06:33:13Z`; fromisoformat only learned to accept
    a trailing `Z` in Python 3.11, and swapping it for `+00:00` works on every
    version.
    """
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def entry_published(entry) -> datetime:
    """Publication time of a feedparser entry, as an aware UTC datetime.

    Some feeds ship entries with no usable date at all. Those are treated as
    "just now" so they are not silently dropped -- deduplication, not the date,
    is what stops us from re-sending them tomorrow.
    """
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Fetchers
# --------------------------------------------------------------------------

def fetch_rss(session: requests.Session, feed: dict[str, str]) -> list[NewsItem]:
    """Fetch and parse one RSS/Atom feed.

    The bytes are fetched with `requests` and only then handed to feedparser,
    rather than letting feedparser fetch the URL itself. That buys us the
    shared session's User-Agent, timeout and retry policy -- feedparser's own
    fetcher has none of those.
    """
    response = session.get(feed["url"], timeout=HTTP_TIMEOUT)
    response.raise_for_status()

    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"malformed feed: {parsed.get('bozo_exception')}")

    items: list[NewsItem] = []
    for entry in parsed.entries:
        link = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        items.append(NewsItem(
            title=title,
            link=link,
            source=feed["name"],
            category=feed["category"],
            published=entry_published(entry),
        ))
    return items


def fetch_hackernews(session: requests.Session,
                     want: int = MAX_ITEMS_PER_FEED) -> list[NewsItem]:
    """Top Hacker News stories via the public Firebase API.

    The endpoint returns ~500 story IDs and each one needs its own request, so
    we walk the list and stop as soon as we have `want` usable stories. Ask HN
    and Show HN posts with no external URL are skipped -- a digest of links
    should link somewhere.
    """
    top = session.get(
        "https://hacker-news.firebaseio.com/v0/topstories.json",
        timeout=HTTP_TIMEOUT,
    )
    top.raise_for_status()
    story_ids = top.json()[:30]   # scan 30 to find `want` with real URLs

    items: list[NewsItem] = []
    for story_id in story_ids:
        if len(items) >= want:
            break
        try:
            detail = session.get(
                f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json",
                timeout=HTTP_TIMEOUT,
            )
            detail.raise_for_status()
            story = detail.json() or {}
        except Exception as exc:      # one bad story is not worth failing over
            log.debug("HN item %s failed: %s", story_id, exc)
            continue

        url = (story.get("url") or "").strip()
        title = (story.get("title") or "").strip()
        if not url or not title or story.get("type") != "story":
            continue

        items.append(NewsItem(
            title=title,
            link=url,
            source="Hacker News",
            category="trending",
            published=datetime.fromtimestamp(story.get("time", 0), tz=timezone.utc),
        ))
    return items


def fetch_github_trending(session: requests.Session,
                          want: int = MAX_ITEMS_PER_FEED) -> list[NewsItem]:
    """New-and-popular GitHub repositories via the official Search API.

    GitHub publishes no API behind github.com/trending, the community mirrors
    of it go offline regularly, and scraping the HTML breaks on every
    redesign. So rather than approximating "trending" we ask a question the
    documented API can answer: repositories created in the last week, ordered
    by stars. Different metric, same purpose, and it will not rot.

    GITHUB_TOKEN is optional -- it only lifts the rate limit from 10 to 30
    search requests per minute, and we make exactly one.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = session.get(
        "https://api.github.com/search/repositories",
        params={
            "q": f"created:>{since} stars:>25",
            "sort": "stars",
            "order": "desc",
            "per_page": want,
        },
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()

    items: list[NewsItem] = []
    for repo in response.json().get("items", [])[:want]:
        description = (repo.get("description") or "").strip()
        title = f"{repo['full_name']} ⭐ {repo.get('stargazers_count', 0)}"
        if description:
            title = f"{title} — {description}"
        items.append(NewsItem(
            title=title,
            link=repo["html_url"],
            source="GitHub Trending",
            category="trending",
            # The repo's real creation time, which is up to 7 days old -- so
            # this source is exempt from MAX_AGE_HOURS (see collect_items).
            # Stamping these with now() instead would make them all equal to
            # the microsecond and leave the display order down to whichever
            # loop iteration ran first.
            published=parse_iso8601(repo.get("created_at")),
        ))
    return items


def collect_items(session: requests.Session) -> list[NewsItem]:
    """Run every source, cap each one, and keep going when something breaks."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    collected: list[NewsItem] = []

    # (label, fetch callable, apply the 48h age filter?)
    sources: list[tuple[str, object, bool]] = [
        *((f["name"], (lambda f=f: fetch_rss(session, f)), True) for f in FEEDS),
        ("Hacker News", (lambda: fetch_hackernews(session)), True),
        # Age-exempt: a repo that charted today was created days ago.
        ("GitHub Trending", (lambda: fetch_github_trending(session)), False),
    ]

    for name, fetch, apply_age_filter in sources:
        try:
            items = fetch()
        except Exception as exc:
            log.warning("source %-20s FAILED  (%s)", name, exc)
            continue

        if apply_age_filter:
            items = [i for i in items if i.published >= cutoff]

        # Newest first, then cap, so each feed keeps its freshest entries.
        items.sort(key=lambda i: i.published, reverse=True)
        items = items[:MAX_ITEMS_PER_FEED]

        log.info("source %-20s ok      (%d item(s))", name, len(items))
        collected.extend(items)

    return collected


# --------------------------------------------------------------------------
# Deduplication state
# --------------------------------------------------------------------------

def load_seen(path: Path) -> list[str]:
    """Read the hash log. A missing or corrupt file means "seen nothing"."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not read %s (%s) -- treating as empty", path.name, exc)
        return []

    if isinstance(data, list):        # tolerate the older bare-array format
        return [str(h) for h in data]
    return [str(h) for h in data.get("hashes", [])]


def save_seen(path: Path, hashes: list[str]) -> None:
    """Write the hash log back, oldest first, trimmed to MAX_SEEN_HASHES.

    Trimming from the front keeps the most recent window, which is all that
    matters: an article that has fallen out of the window is also far outside
    MAX_AGE_HOURS, so the age filter drops it before dedup ever sees it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = hashes[-MAX_SEEN_HASHES:]
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(kept),
        "hashes": kept,
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def drop_seen(items: list[NewsItem], seen: list[str]) -> list[NewsItem]:
    """Remove anything already sent, and anything duplicated inside this run."""
    known = set(seen)
    fresh: list[NewsItem] = []
    for item in items:
        if item.hash in known:
            continue
        known.add(item.hash)          # also dedups within this batch
        fresh.append(item)
    return fresh


def pick_for_digest(items: list[NewsItem],
                    limit: int = MAX_TOTAL_ITEMS) -> list[NewsItem]:
    """Cut down to `limit` items without letting one category take everything.

    A plain "newest 20" cut would be dominated by AWS What's New, which posts
    dozens of entries a day. Instead we round-robin across the categories,
    taking the newest unused item from each in turn, so Security and
    Kubernetes still get a slot on a busy AWS day.
    """
    by_category: dict[str, list[NewsItem]] = {key: [] for key in CATEGORIES}
    for item in items:
        by_category.setdefault(item.category, []).append(item)
    for bucket in by_category.values():
        bucket.sort(key=lambda i: i.published, reverse=True)

    picked: list[NewsItem] = []
    while len(picked) < limit and any(by_category.values()):
        for key in CATEGORIES:
            if len(picked) >= limit:
                break
            if by_category.get(key):
                picked.append(by_category[key].pop(0))
    return picked


# --------------------------------------------------------------------------
# Optional Phase 2: Gemini summaries
# --------------------------------------------------------------------------

def add_summaries(items: list[NewsItem]) -> None:
    """Attach a one-line Thai summary to each item, in place.

    Entirely optional: with no GEMINI_API_KEY the digest just ships without
    summaries. Any failure here is swallowed, because a missing summary is a
    cosmetic problem and a missing digest is not.

    All titles go out in a single request rather than one request per item.
    Gemini's free tier caps flash models at ~15 requests per minute, so 20
    per-item calls would need ~80 seconds of sleeping to stay under the limit
    -- most of the workflow's 2-minute budget. One batched call costs about a
    second. The price is parsing: the model is asked for numbered lines and
    anything that does not come back cleanly numbered is simply left blank.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or not items:
        log.info("Gemini summaries: skipped (no GEMINI_API_KEY)")
        return

    try:
        from google import genai
    except ImportError:
        log.warning("Gemini summaries: skipped (google-genai not installed)")
        return

    numbered = "\n".join(f"{n}. {item.title}" for n, item in enumerate(items, 1))
    prompt = (
        "สรุปพาดหัวข่าว"
        "ไอทีเหล่านี้เป็น"
        "ภาษาไทย หัวข้อละ 1 "
        "บรรทัดสั้นๆ "
        "(ไม่เกิน 100 "
        "ตัวอักษร)\n"
        "ตอบเป็นรายการ"
        "ตัวเลขเท่านั้น "
        "รูปแบบ \"<เลข>. <สรุป>\" "
        "บรรทัดละ 1 "
        "ข่าว ห้ามใส่"
        "ข้อความอื่น\n\n"
        f"{numbered}"
    )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        text = (response.text or "").strip()
    except Exception as exc:
        log.warning("Gemini summaries: skipped (%s)", exc)
        return

    # Map "3. <summary>" back onto items[2]. Lines we cannot parse are dropped.
    filled = 0
    for line in text.splitlines():
        line = line.strip()
        if "." not in line:
            continue
        index_text, _, summary = line.partition(".")
        if not index_text.strip().isdigit():
            continue
        index = int(index_text.strip()) - 1
        summary = summary.strip()
        if 0 <= index < len(items) and summary:
            items[index].summary = summary
            filled += 1

    log.info("Gemini summaries: %d/%d item(s) summarized", filled, len(items))


# --------------------------------------------------------------------------
# Discord formatting
# --------------------------------------------------------------------------

def shorten(text: str, limit: int = MAX_TITLE_CHARS) -> str:
    """Trim to `limit` characters on a word boundary, with an ellipsis."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut.rstrip(" ,.;:-") + "…"


def escape_markdown(text: str) -> str:
    """Neutralize characters that would break a Discord markdown link.

    A `]` or `)` inside a headline closes the link early and the rest of the
    line leaks out as raw text, so those get escaped. Newlines inside a title
    would break the one-item-per-line layout and become spaces.
    """
    text = " ".join(text.split())
    for char in ("[", "]", "(", ")", "*", "_", "~", "`", ">", "|"):
        text = text.replace(char, "\\" + char)
    return text


def render_lines(items: list[NewsItem]) -> list[tuple[NewsItem | None, str]]:
    """Turn picked items into (item, line) pairs, grouped by category.

    Category header rows carry `None` as their item: they are text we need in
    the embed but they are not news, so they must not be recorded as "seen".
    """
    rows: list[tuple[NewsItem | None, str]] = []
    for key, meta in CATEGORIES.items():
        group = [i for i in items if i.category == key]
        if not group:
            continue
        group.sort(key=lambda i: i.published, reverse=True)

        if rows:
            rows.append((None, ""))     # blank line between categories
        rows.append((None, f"**{meta['emoji']} {meta['label']}**"))

        for item in group:
            line = (
                f"{meta['emoji']} [{escape_markdown(shorten(item.title))}]({item.link}) "
                f"— *{escape_markdown(item.source)}*"
            )
            if item.summary:
                line += f"\n　↳ {escape_markdown(item.summary)}"
            rows.append((item, line))
    return rows


def build_embed(items: list[NewsItem]) -> tuple[dict, list[NewsItem]]:
    """Build the Discord embed and report which items actually fit in it.

    Returning the rendered items is the point of this function. Discord caps an
    embed description at 4,096 characters, and whatever gets cut here has NOT
    been delivered -- so it must not be written to seen_urls.json, or it would
    be lost for good. Lines are added only while the whole line fits, and a
    category header with no surviving items is rolled back.
    """
    rows = render_lines(items)

    kept_lines: list[str] = []
    delivered: list[NewsItem] = []
    used = 0
    truncated = False

    for item, line in rows:
        cost = len(line) + (1 if kept_lines else 0)   # the joining newline
        if used + cost > DISCORD_EMBED_DESC_LIMIT:
            truncated = True
            break
        kept_lines.append(line)
        used += cost
        if item is not None:
            delivered.append(item)

    # A category header or blank line left at the end by truncation is noise.
    while kept_lines and (kept_lines[-1] == "" or kept_lines[-1].startswith("**")):
        kept_lines.pop()

    if truncated:
        log.warning("embed truncated: %d of %d item(s) delivered",
                    len(delivered), len(items))

    today = datetime.now(BANGKOK).strftime("%Y-%m-%d")
    embed = {
        "title": f"\U0001F4CB Daily DevOps & Infra News — {today}",
        "description": "\n".join(kept_lines),
        "color": 0x5865F2,            # Discord blurple
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {
            "text": (
                f"\U0001F916 Auto-generated · {len(delivered)} items "
                f"· powered by GitHub Actions"
            )
        },
    }
    return embed, delivered


def send_discord(session: requests.Session, webhook_url: str, embed: dict) -> None:
    """POST the embed to the Discord webhook.

    Raises on failure, and that is deliberate: a non-zero exit fails the
    workflow step, which means the "commit seen_urls.json" step never runs and
    today's items stay unseen. The next run re-sends them instead of silently
    dropping a day of news.
    """
    response = session.post(
        webhook_url,
        json={"embeds": [embed]},
        timeout=HTTP_TIMEOUT,
    )

    # Webhooks are rate limited per channel. At one message a day we will never
    # see this, but a burst of manual test runs can.
    if response.status_code == 429:
        wait = float(response.json().get("retry_after", 2))
        log.warning("Discord rate limited, retrying in %.1fs", wait)
        time.sleep(wait + 0.5)
        response = session.post(
            webhook_url, json={"embeds": [embed]}, timeout=HTTP_TIMEOUT
        )

    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"Discord webhook returned {response.status_code}: {response.text[:300]}"
        )
    log.info("Discord: message delivered (HTTP %s)", response.status_code)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> int:
    # Windows consoles default to a legacy code page and would crash on the
    # emoji in these log lines. Harmless no-op on the Linux runner.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

    if not webhook_url and not dry_run:
        log.error("DISCORD_WEBHOOK_URL is not set (or use DRY_RUN=1 to test)")
        return 1

    session = build_session()

    items = collect_items(session)
    log.info("collected %d item(s) within %dh", len(items), MAX_AGE_HOURS)

    seen = load_seen(SEEN_PATH)
    fresh = drop_seen(items, seen)
    log.info("%d new / %d duplicate(s) against %d stored hash(es)",
             len(fresh), len(items) - len(fresh), len(seen))

    if not fresh:
        # Nothing new is a normal outcome, not an error: all the sources were
        # reachable and we had already sent everything they had. Posting "no
        # news today" every morning would just train people to ignore the
        # channel, so the bot stays quiet and says so in the logs.
        log.info("nothing new to send — exiting without posting")
        return 0

    picked = pick_for_digest(fresh)
    log.info("sending %d item(s) (cap %d)", len(picked), MAX_TOTAL_ITEMS)

    add_summaries(picked)
    embed, delivered = build_embed(picked)

    if dry_run:
        print("\n----- DRY RUN: embed preview -----")
        print(embed["title"])
        print(embed["description"])
        print(embed["footer"]["text"])
        print(f"----- description: {len(embed['description'])}/"
              f"{DISCORD_EMBED_DESC_LIMIT} chars -----\n")
        log.info("DRY_RUN=1 — not posting and not updating seen_urls.json")
        return 0

    send_discord(session, webhook_url, embed)

    # Only now, after a confirmed delivery, does state move forward -- and only
    # for the items that really made it into the message.
    save_seen(SEEN_PATH, seen + [item.hash for item in delivered])
    log.info("recorded %d hash(es) to %s",
             len(delivered), SEEN_PATH.relative_to(REPO_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
