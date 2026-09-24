"""Find an organisation's channels by reading its own website.

For an org with a homepage we:
  1. read the homepage for <link rel="alternate"> feeds and links to social profiles
     (sites link their X, LinkedIn, YouTube and so on in the header or footer)
  2. follow up to three same-site links that look like a newsroom or blog, and look for feeds there
  3. if still no feed, try a few common feed paths (/feed, /rss.xml, ...)
  4. turn a YouTube channel into its RSS feed, a Substack into /feed, a Bluesky profile into /rss
Every feed is test-parsed before it is saved. A newsroom with no feed becomes a page source,
but only if it has enough article-looking links to watch.

Channels found this way are marked origin = 'discovered' and verified, because they came from
the organisation's own site rather than from memory.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from ..db import conn
from ..normalize import canonical_url, utcnow
from .classify import NEWSROOM_HINT, classify, parse_html, site_key, social_profile
from .fetch import article_links, make_client

log = logging.getLogger("news.channels")
SECTION_HINT = re.compile(r"/(news|newsroom|press|blog|announcements|stories|updates|research|insights)(/|$)", re.I)
COMMON_FEEDS = ["/feed", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml", "/blog/rss.xml", "/news/rss.xml", "/blog/feed"]
MIN_PAGE_LINKS = 5


def _get(client: httpx.Client, url: str) -> httpx.Response | None:
    try:
        r = client.get(url, timeout=12)
        return r if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return None


def _is_feed(client: httpx.Client, url: str) -> bool:
    r = _get(client, url)
    if r is None:
        return False
    parsed = feedparser.parse(r.content)
    return bool(parsed.entries) and not (parsed.bozo and not parsed.entries)


def _youtube_feed(client: httpx.Client, profile: str) -> str | None:
    if "/channel/" in profile:
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={profile.rsplit('/', 1)[-1]}"
    r = _get(client, profile)
    if r is None:
        return None
    m = re.search(r"feeds/videos\.xml\?channel_id=(UC[\w-]{20,})", r.text) or re.search(r'"channelId":"(UC[\w-]{20,})"', r.text)
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}" if m else None


def find_channels(homepage: str, client: httpx.Client) -> list[dict]:
    """Returns channel dicts: {url, channel, kind}. Pure discovery, writes nothing."""
    found: dict[str, dict] = {}

    def add(url: str, channel: str, kind: str):
        key = canonical_url(url)
        if key not in found:
            found[key] = {"url": url, "channel": channel, "kind": kind}

    home = _get(client, homepage)
    if home is None:
        raise ValueError(f"couldn't read {homepage}")
    base = str(home.url)
    page = parse_html(home.text, base)
    for feed in page.feeds:
        if _is_feed(client, feed):
            add(feed, classify(feed)[0], "rss")

    sections: list[str] = []
    for href, _text in page.links:
        prof = social_profile(href)
        if prof:
            channel, url = prof
            if channel == "youtube":
                feed = _youtube_feed(client, url)
                add(url, "youtube", "none")
                if feed:
                    add(feed, "youtube", "rss")
            else:
                ch, kind, stored = classify(url)
                add(stored, ch, kind)
                if ch in ("substack", "bluesky"):
                    add(url, ch, "none")  # keep the clickable profile too
            continue
        if site_key(href) == site_key(base) and SECTION_HINT.search(urlsplit(href).path + "/"):
            path = urlsplit(href).path.rstrip("/")
            if path.count("/") <= 2 and href not in sections:
                sections.append(href.split("#")[0])

    for section in sections[:3]:
        r = _get(client, section)
        if r is None:
            continue
        sp = parse_html(r.text, str(r.url))
        feeds = [f for f in sp.feeds if _is_feed(client, f)]
        channel = "newsroom" if NEWSROOM_HINT.search(urlsplit(section).path + "/") else classify(section)[0]
        for feed in feeds:
            add(feed, channel, "rss")
        if not feeds and len(article_links(r.text, str(r.url))) >= MIN_PAGE_LINKS:
            add(str(r.url), channel, "page")

    if not any(c["kind"] == "rss" and c["channel"] not in ("youtube", "substack", "bluesky") for c in found.values()):
        for path in COMMON_FEEDS:
            url = urljoin(base, path)
            if _is_feed(client, url):
                add(url, classify(url)[0], "rss")
                break
    return list(found.values())


def discover_for_org(org_id: int, client: httpx.Client | None = None) -> dict:
    """Run discovery for one org and save anything new. Returns a summary for the UI and logs."""
    with conn() as c:
        org = c.execute("select * from orgs where id = %s", (org_id,)).fetchone()
    if not org or not org["homepage"]:
        return {"org": org and org["name"], "added": [], "error": "no homepage set"}
    own = client is None
    client = client or make_client()
    try:
        channels = find_channels(org["homepage"], client)
        error = None
    except Exception as exc:  # noqa: BLE001
        channels, error = [], str(exc)
    finally:
        if own:
            client.close()
    added = []
    check_hours = 24 if org["priority"] == "watch" else 0
    with conn() as c:
        for ch in channels:
            row = c.execute(
                """insert into sources (topic, name, url, homepage, kind, channel, tier, org_id, origin, verified_at,
                                        check_every_hours)
                   values (null, %s, %s, %s, %s, %s, %s, %s, 'discovered', now(), %s)
                   on conflict (url) do update set verified_at = now(),
                     org_id = coalesce(sources.org_id, excluded.org_id)
                   returning (xmax = 0) as inserted""",
                (f"{org['name']} {ch['channel']}", ch["url"], org["homepage"], ch["kind"], ch["channel"],
                 org["default_tier"], org["id"], check_hours),
            ).fetchone()
            if row["inserted"]:
                added.append(ch)
        c.execute("update orgs set channels_discovered_at = %s where id = %s", (utcnow(), org["id"]))
    log.info("channel discovery for %s: %d found, %d new%s", org["name"], len(channels), len(added),
             f", error: {error}" if error else "")
    return {"org": org["name"], "found": len(channels), "added": added, "error": error}


def discover_pending(limit: int, client: httpx.Client | None = None) -> list[dict]:
    """Discovery for followed orgs that have never had it, a few per scan."""
    if limit <= 0:
        return []
    with conn() as c:
        ids = [r["id"] for r in c.execute(
            """select id from orgs where status = 'following' and homepage is not null
               and channels_discovered_at is null order by priority, id limit %s""", (limit,)).fetchall()]
    return [discover_for_org(i, client) for i in ids]
