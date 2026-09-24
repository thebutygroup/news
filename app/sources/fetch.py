"""Readers. One function per read mode; each takes a source row and returns candidate items.

Cheap by design:
  - Conditional GET: we send the ETag / Last-Modified from last time. An unchanged feed costs a 304.
  - Page sources remember every article link they have seen (source_links), so only new ones count.
    The first read of a page is a baseline: links are recorded, nothing is posted.
  - Only the first few new links on a page get a second request for their title and date.
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import feedparser
import httpx

from ..config import settings
from ..db import conn
from ..normalize import canonical_url, clean_text, domain, parse_iso
from .classify import parse_html, site_key

log = logging.getLogger("news.fetch")

USER_AGENT = "Mozilla/5.0 (compatible; NewsFeedBot/1.0; +https://news.thebutygroup.com)"
SKIP_SEGMENTS = re.compile(
    r"/(tag|tags|category|categories|topic|topics|author|authors|page|search|login|signin|signup|register|"
    r"privacy|terms|legal|cookies?|careers|jobs|contact|about|subscribe|newsletter|events|feed|rss)(/|$)", re.I)
SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|gif|svg|webp|css|js|zip|mp3|mp4|ico|woff2?)$", re.I)
ARTICLE_HINT = re.compile(r"(/20\d\d/|/\d{4}-\d{2}-\d{2}|[a-z0-9]+-[a-z0-9]+-[a-z0-9]+-[a-z0-9]+)", re.I)


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(timeout=20, follow_redirects=True, transport=transport,
                        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-GB,en;q=0.8"})


def conditional_get(client: httpx.Client, source: dict) -> httpx.Response | None:
    """GET with cache headers. Returns None when the server says nothing changed."""
    headers = {}
    if source.get("etag"):
        headers["If-None-Match"] = source["etag"]
    if source.get("last_modified"):
        headers["If-Modified-Since"] = source["last_modified"]
    resp = client.get(source["url"], headers=headers)
    if resp.status_code == 304:
        return None
    resp.raise_for_status()
    with conn() as c:
        c.execute("update sources set etag = %s, last_modified = %s where id = %s",
                  (resp.headers.get("etag"), resp.headers.get("last-modified"), source["id"]))
    return resp


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def _item(source: dict, url: str, title: str, excerpt: str, published, found_via: str) -> dict:
    return {
        "url": url,
        "title": title,
        "excerpt": excerpt,
        "source_name": source.get("display_name") or source["name"],
        "source_id": source["id"],
        "org_id": source.get("org_id"),
        "tier": source["tier"],
        "published_at": published,
        "found_via": found_via,
    }


# ---- rss ---------------------------------------------------------------------
def fetch_feed(source: dict, since: datetime, client: httpx.Client) -> list[dict]:
    url = source["url"]
    if url.startswith("file://"):
        parsed = feedparser.parse(url[len("file://"):])
    else:
        resp = conditional_get(client, source)
        if resp is None:
            return []
        parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"not a readable feed: {parsed.get('bozo_exception')}")
    items = []
    for entry in parsed.entries[: settings.max_items_per_feed]:
        link, title = entry.get("link"), clean_text(entry.get("title"), 300)
        if not link or not title:
            continue
        published = _entry_time(entry)
        if published and published < since:
            continue
        items.append(_item(source, link, title, clean_text(entry.get("summary") or entry.get("description"), 600),
                           published, "feed"))
    return items


# ---- page --------------------------------------------------------------------
def article_links(html: str, base_url: str, pattern: str | None = None) -> list[tuple[str, str]]:
    """Links on a listing page that look like articles on the same site, in page order."""
    parsed = parse_html(html, base_url)
    base_key, base_path = site_key(base_url), urlsplit(base_url).path.rstrip("/")
    rx = re.compile(pattern) if pattern else None
    out, seen = [], set()
    for href, text in parsed.links:
        if not href.startswith(("http://", "https://")):
            continue
        if site_key(href) != base_key:
            continue
        path = urlsplit(href).path.rstrip("/")
        if not path or path == base_path or SKIP_EXT.search(path):
            continue
        if rx:
            if not rx.search(href):
                continue
        else:
            if SKIP_SEGMENTS.search(path + "/"):
                continue
            deep = path.count("/") >= 2 and len(path.split("/")[-1]) >= 8
            if not (deep or ARTICLE_HINT.search(path)):
                continue
        key = canonical_url(href)
        if key in seen:
            continue
        seen.add(key)
        out.append((href, text))
    return out


def page_metadata(client: httpx.Client, url: str) -> dict:
    try:
        resp = client.get(url)
        resp.raise_for_status()
    except Exception:  # noqa: BLE001 - metadata is a nice-to-have
        return {}
    p = parse_html(resp.text[:400_000], url)
    published = (p.meta.get("article:published_time") or p.meta.get("datepublished")
                 or p.meta.get("parsely-pub-date") or (p.times[0] if p.times else None))
    return {
        "title": clean_text(p.meta.get("og:title") or p.meta.get("twitter:title") or p.title, 300),
        "excerpt": clean_text(p.meta.get("og:description") or p.meta.get("description") or p.meta.get("twitter:description"), 600),
        "published": parse_iso(published),
    }


def fetch_page(source: dict, since: datetime, client: httpx.Client) -> list[dict]:
    resp = conditional_get(client, source)
    if resp is None:
        return []
    links = article_links(resp.text, str(resp.url), source.get("link_pattern"))
    if not links:
        raise ValueError("no article links found. The page may need JavaScript, or set a link pattern for it.")
    with conn() as c:
        known = {r["canonical_url"] for r in c.execute(
            "select canonical_url from source_links where source_id = %s", (source["id"],)).fetchall()}
        baseline = not known
        fresh = [(u, t) for u, t in links if canonical_url(u) not in known]
        for u, _ in fresh:
            c.execute("insert into source_links (source_id, canonical_url) values (%s, %s) on conflict do nothing",
                      (source["id"], canonical_url(u)))
    if baseline:
        log.info("baseline for %s: %d links recorded", source["name"], len(fresh))
        return []
    items = []
    for i, (url, text) in enumerate(fresh[:20]):
        meta = page_metadata(client, url) if i < settings.page_metadata_per_source else {}
        published = meta.get("published")
        if published and published < since:
            continue  # an old article resurfaced in a "related" widget
        title = meta.get("title") or text or urlsplit(url).path.rsplit("/", 1)[-1].replace("-", " ")
        items.append(_item(source, url, clean_text(title, 300), meta.get("excerpt", ""), published, "page"))
    return items


# ---- hacker news -------------------------------------------------------------
def fetch_hn(source: dict, since: datetime, client: httpx.Client) -> list[dict]:
    params = {
        "query": source.get("query") or "",
        "tags": "story",
        "numericFilters": f"created_at_i>{int(since.timestamp())},points>{source.get('min_points') or 50}",
        "hitsPerPage": settings.max_items_per_feed,
    }
    resp = client.get("https://hn.algolia.com/api/v1/search_by_date", params=params)
    resp.raise_for_status()
    items = []
    for hit in resp.json().get("hits", []):
        hn_link = f"https://news.ycombinator.com/item?id={hit['objectID']}"
        url = hit.get("url") or hn_link
        item = _item(source, url, clean_text(hit.get("title"), 300),
                     f"{hit.get('points', 0)} points and {hit.get('num_comments', 0)} comments on Hacker News ({hn_link}).",
                     datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc), "hn")
        item["source_name"] = domain(url) if hit.get("url") else "Hacker News"
        items.append(item)
    return items
