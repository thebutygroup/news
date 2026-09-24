"""One scan, start to finish. Every decision is logged to candidates so the runs page can show it."""
from __future__ import annotations

import json
import logging
import traceback

from ..config import load_topics, settings
from ..db import SCAN_LOCK, conn
from ..normalize import canonical_url, utcnow
from ..sources import relevance
from ..sources.channels import discover_pending
from .cluster import cluster_and_post, materialize_story_tags, merge_duplicate_stories
from .collect import collect_watchlist, discover, lookback_since
from .curate import curate
from .llm import BudgetExceeded, get_llm

log = logging.getLogger("news.run")


def _store_candidates(run_id: int, topic_slug: str, items: list[dict], stats: dict) -> list[dict]:
    """Layer 1 dedup: a canonical URL we have ever seen, in any topic, is skipped."""
    fresh: list[dict] = []
    seen_in_batch: set[str] = set()
    # When the same URL arrives by several routes, keep the most trustworthy one.
    order = {"feed": 0, "page": 1, "x": 2, "hn": 3, "search": 4}
    items = sorted(items, key=lambda i: order.get(i["found_via"], 9))
    with conn() as c:
        for item in items:
            key = canonical_url(item["url"])
            if key in seen_in_batch or not item.get("title"):
                continue
            seen_in_batch.add(key)
            row = c.execute(
                """insert into candidates (run_id, topic, url, canonical_url, title, excerpt, source_name, source_id,
                                           org_id, tier, published_at, found_via)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   on conflict (canonical_url) do nothing returning id""",
                (run_id, topic_slug, item["url"], key, item["title"], item.get("excerpt"), item.get("source_name"),
                 item.get("source_id"), item.get("org_id"), item.get("tier", 1), item.get("published_at"),
                 item["found_via"]),
            ).fetchone()
            if row:
                fresh.append({**item, "id": row["id"], "canonical_url": key})
    stats["found"] = stats.get("found", 0) + len(items)
    stats["new"] = stats.get("new", 0) + len(fresh)
    return fresh


def _apply_cap(fresh: list[dict], stats: dict) -> list[dict]:
    cap = settings.max_candidates_per_run
    if len(fresh) <= cap:
        return fresh
    order = {"search": 0, "feed": 1, "hn": 2}
    ranked = sorted(fresh, key=lambda i: (i.get("tier", 1), order.get(i["found_via"], 3),
                                          -(i["published_at"] or utcnow()).timestamp()))
    keep, overflow = ranked[:cap], ranked[cap:]
    with conn() as c:
        c.execute("update candidates set decision = 'skipped', reason = 'over the per-run candidate cap' where id = any(%s)",
                  ([i["id"] for i in overflow],))
    stats["skipped_over_cap"] = len(overflow)
    return keep


def _source_upkeep(stats: dict) -> None:
    """After posting: refresh which orgs matter, propose new ones, find channels for a few."""
    relevance.update_mentions()
    proposed = relevance.propose_from_mentions()
    if proposed:
        stats["orgs_proposed"] = proposed
    try:
        found = discover_pending(settings.channel_discovery_per_run)
        if found:
            stats["channel_discovery"] = [{"org": f["org"], "added": len(f["added"]), "error": f.get("error")} for f in found]
    except Exception as exc:  # noqa: BLE001 - discovery is housekeeping, never fail the scan for it
        log.warning("channel discovery failed: %s", exc)


def run_scan(trigger: str = "schedule") -> dict:
    with conn() as lock_conn:
        got = lock_conn.execute("select pg_try_advisory_lock(%s) as ok", (SCAN_LOCK,)).fetchone()["ok"]
        if not got:
            log.info("A scan is already running, skipping")
            return {"skipped": "already running"}
        try:
            return _run(trigger)
        finally:
            lock_conn.execute("select pg_advisory_unlock(%s)", (SCAN_LOCK,))


def _run(trigger: str) -> dict:
    with conn() as c:
        first_run = c.execute("select count(*) as n from runs where status = 'ok'").fetchone()["n"] == 0
        run_id = c.execute("insert into runs (trigger) values (%s) returning id", (trigger,)).fetchone()["id"]
    stats: dict = {}
    status, error = "ok", None
    llm = None
    try:
        llm = get_llm()
        since = lookback_since(first_run)
        hours = int((utcnow() - since).total_seconds() // 3600)
        for topic in load_topics():
            tstats: dict = {}
            items = collect_watchlist(topic, since, tstats)
            try:
                items += discover(llm, topic, hours, tstats)
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 - discovery failing should not lose the watchlist
                log.exception("discovery failed")
                tstats["discovery_error"] = str(exc)[:300]
            fresh = _apply_cap(_store_candidates(run_id, topic.slug, items, tstats), tstats)
            kept = curate(llm, topic, fresh, tstats)
            cluster_and_post(llm, topic, kept, tstats)
            stats[topic.slug] = tstats
        merge_duplicate_stories(llm, stats)
        materialize_story_tags(stats)
        _source_upkeep(stats)
    except BudgetExceeded as exc:
        status, error = "error", str(exc)
    except Exception as exc:  # noqa: BLE001
        log.exception("scan failed")
        status, error = "error", "".join(traceback.format_exception_only(type(exc), exc)).strip()[:2000]
    finally:
        if llm is not None:
            stats["llm"] = llm.usage
        with conn() as c:
            c.execute("update runs set finished_at = now(), status = %s, stats = %s, error = %s where id = %s",
                      (status, json.dumps(stats, default=str), error, run_id))
    log.info("run %s finished: %s %s", run_id, status, json.dumps(stats, default=str))
    return {"run_id": run_id, "status": status, "stats": stats, "error": error}
