"""Stage 1: find candidate items.

Routes, in order of trust:
  feed    RSS/Atom channels (company newsrooms, blogs, Substacks, YouTube, Bluesky, ...)
  page    newsroom pages with no feed, watched for new article links
  x       official X accounts, only if X_BEARER_TOKEN is set
  hn      Hacker News stories above a points threshold
  search  Claude searches the web, including a sweep of followed orgs nothing else covers

Channels are read in parallel (FETCH_WORKERS). A channel checked less than check_every_hours ago
is skipped. Every read updates the channel's health; one broken channel never stops the scan.
"""
from __future__ import annotations

import logging
import string
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from ..config import Topic, settings
from ..db import conn
from ..normalize import canonical_url, clean_text, domain, parse_iso, utcnow
from ..sources.fetch import fetch_feed, fetch_hn, fetch_page, make_client
from ..sources.relevance import note_search_hits, sweep_list
from ..sources.xapi import XBudget, fetch_x

log = logging.getLogger("news.collect")
PROMPTS = Path(__file__).resolve().parent / "prompts"
READERS = {"rss": fetch_feed, "page": fetch_page, "hn": fetch_hn}

DISCOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "source_name": {"type": "string"},
                    "published": {"type": ["string", "null"]},
                    "kind": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["url", "title", "source_name"],
            },
        }
    },
    "required": ["items"],
}


def _record_health(source_id: int, error: str | None, new_items: int) -> None:
    with conn() as c:
        if error:
            c.execute(
                """update sources set fail_count = fail_count + 1, last_error = %s, last_fetched_at = now(),
                   status = case when fail_count + 1 >= %s then 'paused' else status end
                   where id = %s""",
                (error[:500], settings.source_fail_pause_after, source_id),
            )
        else:
            c.execute(
                """update sources set fail_count = 0, last_error = null, last_fetched_at = now(), verified_at = now(),
                   last_new_item_at = case when %s > 0 then now() else last_new_item_at end where id = %s""",
                (new_items, source_id),
            )


def due_sources(topic: Topic) -> list[dict]:
    """Active, readable channels for this topic: topic feeds plus channels of followed orgs."""
    with conn() as c:
        return c.execute(
            """select s.*, o.name as org_name, o.kind as org_kind,
                      coalesce(o.name, s.name) as display_name
               from sources s left join orgs o on o.id = s.org_id
               where s.status = 'active' and s.kind in ('rss', 'page', 'hn', 'x')
                 and (s.kind <> 'x' or %(x)s)
                 and (s.topic = %(t)s or (o.status = 'following' and %(t)s = any(o.topics)))
                 and (s.check_every_hours = 0 or s.last_fetched_at is null
                      or s.last_fetched_at < now() - make_interval(hours => s.check_every_hours))
               order by s.id""",
            {"t": topic.slug, "x": bool(settings.x_bearer_token)},
        ).fetchall()


def collect_watchlist(topic: Topic, since: datetime, stats: dict, client=None) -> list[dict]:
    sources = due_sources(topic)
    budget = XBudget(settings.x_max_reads_per_run)
    own = client is None
    client = client or make_client()

    def read(source):
        if source["kind"] == "x":
            return fetch_x(source, since, client, budget)
        return READERS[source["kind"]](source, since, client)

    items: list[dict] = []
    try:
        with ThreadPoolExecutor(max_workers=max(1, settings.fetch_workers)) as pool:
            futures = {pool.submit(read, s): s for s in sources}
            for fut in as_completed(futures):
                source = futures[fut]
                try:
                    got = fut.result()
                    _record_health(source["id"], None, len(got))
                    stats["sources_ok"] = stats.get("sources_ok", 0) + 1
                    items.extend(got)
                except Exception as exc:  # noqa: BLE001 - one bad channel must not stop the scan
                    log.warning("source %s failed: %s", source["name"], exc)
                    _record_health(source["id"], f"{type(exc).__name__}: {exc}", 0)
                    stats["sources_failed"] = stats.get("sources_failed", 0) + 1
    finally:
        if own:
            client.close()
    stats["sources_checked"] = len(sources)
    if budget.used:
        stats["x_posts_read"] = budget.used
    return items


def _render(template: str, **values) -> str:
    return string.Template(template).safe_substitute(**values)


def discover(llm, topic: Topic, hours: int, stats: dict) -> list[dict]:
    """Ask the model to search the web. Only URLs that appeared in real search results survive."""
    if not topic.discovery_queries or settings.max_searches_per_run <= 0:
        return []
    sweep = sweep_list(topic.slug)
    prompt = _render(
        (PROMPTS / "discovery.md").read_text(),
        topic_label=topic.label,
        topic_description=topic.description,
        lens=topic.lens.strip(),
        today=utcnow().date().isoformat(),
        hours=str(hours),
        queries="\n".join(f"- {q}" for q in topic.discovery_queries),
        sweep="\n".join(f"- {o['name']}" + (f" ({o['homepage']})" if o["homepage"] else "") for o in sweep)
              or "- None this scan.",
    )
    report, seen = llm.search_and_report(
        model=settings.discovery_model,
        system="You are a precise research scout. You report only what you verified in search results.",
        prompt=prompt,
        schema=DISCOVERY_SCHEMA,
        max_searches=settings.max_searches_per_run,
    )
    items, dropped = [], 0
    for found in report.get("items", []):
        url = (found.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            dropped += 1
            continue
        if seen and canonical_url(url) not in seen:
            dropped += 1  # the model reported a URL it never actually saw
            continue
        items.append({
            "url": url,
            "title": clean_text(found.get("title"), 300),
            "excerpt": clean_text(found.get("why"), 300),
            "source_name": clean_text(found.get("source_name"), 120) or domain(url),
            "source_id": None,
            "org_id": None,
            "tier": 2,
            "published_at": parse_iso(found.get("published")),
            "found_via": "search",
        })
    stats["discovery_reported"] = stats.get("discovery_reported", 0) + len(report.get("items", []))
    stats["discovery_unverified_dropped"] = stats.get("discovery_unverified_dropped", 0) + dropped
    stats["orgs_swept"] = len(sweep)
    _attribute_to_orgs(items)
    note_search_hits(topic.slug, items)
    return items


def _attribute_to_orgs(items: list[dict]) -> None:
    """A search result on a followed org's own site counts as that org's official post."""
    with conn() as c:
        by_host = {}
        for r in c.execute("select id, homepage from orgs where status = 'following' and homepage is not null").fetchall():
            by_host.setdefault(domain(r["homepage"]), r["id"])
    for item in items:
        item["org_id"] = by_host.get(domain(item["url"]))


def lookback_since(first_run: bool) -> datetime:
    hours = settings.first_run_lookback_hours if first_run else settings.lookback_hours
    return utcnow() - timedelta(hours=hours)
