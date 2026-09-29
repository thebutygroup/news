"""Twice-daily briefings: a morning and an afternoon edition, each covering only what's new.

After each scheduled scan (04:00 and 13:00 UK), the worker writes an edition: morning if the scan
finished before noon, afternoon after. An edition covers the stories posted since the previous
edition ended (its window), so nothing is summarised twice, and it's shown the previous edition so
the writing doesn't repeat it either. Manual scans don't make editions, so testing can't spend.

    python -m app.digest                         make the edition for now (morning before noon)
    python -m app.digest --edition afternoon     a specific edition today
    python -m app.digest --day 2026-09-29 --edition morning --force   redo one (costs money)

A morning edition covers up to noon at most, an afternoon one up to midnight, so both of a day's
editions can be made late (morning first, then afternoon) and still split the day cleanly.
    add --dry-run to any of these to see what would happen and spend nothing

Steps: gather the window's stories, fetch full article text for the top ones (used once, never
stored), have Claude (DIGEST_MODEL) write the edition, then (PODCAST_ENABLED) turn it into a two-host
script and record it. Cost guards: an edition that exists is left alone; missing audio is recorded
from the saved script; --force redoes it with the same window, so the content stays the same stories.
"""
from __future__ import annotations

import json
import logging
import string
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import settings
from .db import conn
from .normalize import utcnow

log = logging.getLogger("news.digest")
PROMPT = Path(__file__).resolve().parent / "pipeline" / "prompts" / "digest.md"
EDITIONS = ("morning", "afternoon")

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "paragraphs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "post_ids": {"type": "array", "items": {"type": "integer"}}},
                "required": ["text"],
            },
        },
    },
    "required": ["headline", "paragraphs"],
}


def tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def day_label(day: date) -> str:
    return day.strftime("%A %d %B %Y").replace(" 0", " ")


def edition_for(moment: datetime | None = None) -> tuple[date, str]:
    local = (moment or datetime.now(tz())).astimezone(tz())
    return local.date(), ("morning" if local.hour < 12 else "afternoon")


def existing(day: date, edition: str) -> dict | None:
    with conn() as c:
        return c.execute("select * from episodes where day = %s and coalesce(edition, 'day') = %s",
                         (day, edition)).fetchone()


def previous_edition(before: datetime) -> dict | None:
    with conn() as c:
        return c.execute(
            """select * from episodes where status = 'ok' and window_end is not null and window_end <= %s
               order by window_end desc limit 1""", (before,)).fetchone()


def stories_in_window(start: datetime, end: datetime) -> list[dict]:
    """One entry per story with a post in the window. A story that started before the window is
    marked continuing, and brings only its new posts' "what's new" lines."""
    with conn() as c:
        return c.execute(
            """select * from (
                 select distinct on (coalesce(p.story_id, -p.id)) p.id, p.story_id, p.title, p.summary, p.details,
                        p.url, p.source_name, p.importance, p.posted_at,
                        exists(select 1 from posts p0 where p0.story_id = p.story_id and p0.posted_at <= %(s)s) as continuing,
                        (select string_agg(p2.delta, ' ') from posts p2 where p2.story_id = p.story_id
                           and p2.delta is not null and p2.posted_at > %(s)s and p2.posted_at <= %(e)s) as new_in_window,
                        (select count(*) from coverage cv where cv.story_id = p.story_id) as outlets,
                        (select count(*) from votes v where v.post_id = p.id) as votes
                 from posts p
                 where not p.hidden and p.posted_at > %(s)s and p.posted_at <= %(e)s
                   and not exists (select 1 from feedback f where f.post_id = p.id)
                 order by coalesce(p.story_id, -p.id), p.posted_at
               ) s
               order by importance * 3 + least(outlets, 5) + votes desc, posted_at""",
            {"s": start, "e": end},
        ).fetchall()


def fetch_full_text(stories: list[dict], client=None) -> None:
    """Adds `full_text` to the top stories, in place. Paywalls and failures just leave it out."""
    import trafilatura

    from .sources.fetch import is_public_url, make_client

    top = [s for s in stories[: settings.digest_fulltext_stories] if is_public_url(s["url"])]
    if not top:
        return
    own = client is None
    client = client or make_client()

    def grab(story):
        try:
            r = client.get(story["url"])
            r.raise_for_status()
            text = trafilatura.extract(r.text, include_comments=False, include_tables=False) or ""
            return text[: settings.digest_fulltext_chars]
        except Exception:  # noqa: BLE001
            return ""

    try:
        with ThreadPoolExecutor(max_workers=max(1, settings.fetch_workers)) as pool:
            for story, text in zip(top, pool.map(grab, top)):
                if len(text) > 400:
                    story["full_text"] = text
    finally:
        if own:
            client.close()




def write_edition(llm, day: date, edition: str, stories: list[dict], prev: dict | None) -> dict:
    system = string.Template(PROMPT.read_text()).safe_substitute(
        edition=edition, day=day_label(day), words=str(settings.digest_words), max_words=str(settings.digest_max_words))
    previous = None
    if prev:
        previous = {"edition": f"{prev['edition'] or 'whole day'} of {prev['day']}", "headline": prev["headline"],
                    "text": " ".join(p["text"] for p in prev["summary"] or [])}
    result = llm.structured(
        model=settings.digest_model, system=system, tool_name="digest_summary", schema=SCHEMA, max_tokens=4000,
        payload={"day": day_label(day), "edition": edition, "previous_edition": previous,
                 "stories": [{k: v for k, v in {
                     "id": s["id"], "headline": s["title"], "source": s["source_name"], "summary": s["summary"],
                     "details": s["details"], "continuing": s["continuing"] or None, "new": s["new_in_window"],
                     "other_outlets": s["outlets"], "importance": s["importance"],
                     "full_text": s.get("full_text")}.items() if v not in (None, "")}
                     for s in stories]},
    )
    valid = {s["id"] for s in stories}
    paragraphs = [{"text": p["text"].strip(), "post_ids": [i for i in p.get("post_ids", []) if i in valid]}
                  for p in result.get("paragraphs", []) if p.get("text", "").strip()]
    if not paragraphs:
        raise RuntimeError("the summary came back empty")
    return {"headline": (result.get("headline") or f"AI news, {day_label(day)}").strip()[:160], "paragraphs": paragraphs}


def make_edition(day: date | None = None, edition: str | None = None, llm=None, audio: bool | None = None,
                 force: bool = False, audio_only: bool = False, new_script: bool = False,
                 dry_run: bool = False) -> dict:
    from .pipeline.llm import get_llm

    now = utcnow()
    if day is None or edition is None:
        d, e = edition_for()
        day, edition = day or d, edition or e
    if edition not in EDITIONS:
        raise ValueError(f"edition must be one of {EDITIONS}")
    audio_only = audio_only or new_script
    want_audio = settings.podcast_enabled if audio is None else audio
    row = existing(day, edition)
    have_summary = bool(row and row["status"] == "ok" and row["summary"])
    have_audio = bool(row and row["audio_file"])
    redo_summary = (force and not audio_only) or not have_summary
    redo_audio = want_audio and (force or audio_only or redo_summary or not have_audio)

    # The window: reuse the stored one when redoing. Otherwise it runs from the end of the previous
    # edition to now, but a morning edition never reaches past noon and an afternoon one never past
    # midnight, so both editions for a day can be made after the fact without swallowing each other.
    if row and row["window_start"] and row["window_end"]:
        start, end = row["window_start"], row["window_end"]
        prev = previous_edition(start)
    else:
        noon = datetime.combine(day, time(12, 0), tz())
        end = min(now, noon if edition == "morning" else datetime.combine(day + timedelta(days=1), time.min, tz()))
        prev = previous_edition(end)
        start = prev["window_end"] if prev else end - timedelta(hours=24)
        if start >= end:
            return {"day": str(day), "edition": edition, "skipped": "nothing to cover yet: this edition's window hasn't started"}

    plan = {"day": str(day), "edition": edition, "window": [start.isoformat(), end.isoformat()],
            "summary": "write" if redo_summary else "keep",
            "audio": "record" if redo_audio else ("keep" if have_audio else "off"),
            "script": ("reuse saved" if redo_audio and row and row["script"] and not (force or new_script or redo_summary)
                       else "write" if redo_audio else "-")}
    if dry_run:
        stories = stories_in_window(start, end)
        plan.update({"stories": len(stories), "full_text_candidates": min(len(stories), settings.digest_fulltext_stories),
                     "tts": settings.podcast_tts, "tts_chars_estimate": settings.podcast_words * 6 if redo_audio else 0,
                     "tts_chars_this_month": _chars_this_month()})
        return {"dry_run": True, **plan}
    if not redo_summary and not redo_audio:
        return {**plan, "skipped": "already done; pass --force to redo it (that costs a Claude call and audio)"}

    llm = llm or get_llm()
    result = {**plan}
    stories = stories_in_window(start, end)
    if redo_summary:
        if not stories:
            log.info("nothing new since the last edition, no %s edition", edition)
            return {**plan, "skipped": "nothing new since the previous edition"}
        fetch_full_text(stories)
        try:
            written = write_edition(llm, day, edition, stories, prev)
        except Exception as exc:  # noqa: BLE001
            log.exception("edition failed")
            return {**plan, "error": str(exc)}
        cited = {i for p in written["paragraphs"] for i in p["post_ids"]}
        sources = [{"id": s["id"], "title": s["title"], "url": s["url"], "source": s["source_name"]}
                   for s in stories if s["id"] in cited]
        notes = "\n".join(f"{s['title']} ({s['source'] or 'source'}): {s['url']}" for s in sources)
        with conn() as c:
            row = c.execute(
                """insert into episodes (day, edition, window_start, window_end, title, headline, summary, summary_model,
                                         sources, script, post_ids, notes, status)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, '[]', %s, %s, 'ok')
                   on conflict (day, (coalesce(edition, 'day'))) where day is not null do update set
                     title = excluded.title, headline = excluded.headline, summary = excluded.summary,
                     summary_model = excluded.summary_model, sources = excluded.sources, script = '[]',
                     post_ids = excluded.post_ids, notes = excluded.notes, status = 'ok', error = null
                   returning *""",
                (day, edition, start, end, written["headline"], written["headline"], json.dumps(written["paragraphs"]),
                 settings.digest_model, json.dumps(sources), [s["id"] for s in stories], notes),
            ).fetchone()
        result.update({"headline": written["headline"], "stories": len(stories),
                       "full_text": sum(1 for s in stories if s.get("full_text")),
                       "words": sum(len(p["text"].split()) for p in written["paragraphs"])})
    else:
        written = {"headline": row["headline"], "paragraphs": row["summary"]}
    result["id"] = row["id"]

    if redo_audio:
        from . import podcast

        try:
            segments = row["script"] if (row["script"] and not (force or new_script or redo_summary)) else None
            if not segments:
                segments = podcast.write_script(llm, day_label(day), written, stories, edition=edition)
                with conn() as c:
                    c.execute("update episodes set script = %s where id = %s", (json.dumps(segments), row["id"]))
            podcast.check_budget(segments)
            chars = podcast.script_chars(segments)
            podcast.save_audio(row["id"], podcast.synthesize(segments), chars)
            result.update({"audio": "ok", "tts_chars": chars})
        except Exception as exc:  # noqa: BLE001 - the written edition stands even if audio fails
            log.exception("audio failed")
            with conn() as c:
                c.execute("update episodes set audio_error = %s where id = %s", (str(exc)[:1000], row["id"]))
            result["audio"] = f"error: {exc}"
    result["llm"] = getattr(llm, "usage", {})
    log.info("%s edition for %s: %s", edition, day, result)
    return result


def after_scheduled_scan() -> dict | None:
    """Called when a scheduled scan finishes: make the edition for this time of day."""
    if not settings.digest_enabled:
        return None
    return make_edition()


def _chars_this_month() -> int:
    from .podcast import chars_this_month

    return chars_this_month()


def recent(limit: int = 120) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """select id, day, edition, headline, summary, sources, duration_seconds, audio_file, bytes, window_end
               from episodes where day is not null and status = 'ok'
               order by day desc, window_end desc nulls last limit %s""", (limit,)).fetchall()
    return [{"day": str(r["day"]), "edition": r["edition"], "headline": r["headline"], "paragraphs": r["summary"],
             "sources": r["sources"] or [],
             "audio_url": f"/podcast/{r['id']}.mp3?v={r['bytes']}" if r["audio_file"] else None,
             "duration_seconds": r["duration_seconds"],
             "covers_until": r["window_end"].isoformat() if r["window_end"] else None} for r in rows]


if __name__ == "__main__":
    import argparse

    from .seed import bootstrap

    parser = argparse.ArgumentParser(description="Write a briefing edition and its audio. Only does what's missing.")
    parser.add_argument("--day", help="YYYY-MM-DD, default today (UK time)")
    parser.add_argument("--edition", choices=EDITIONS, help="default: morning before noon, afternoon after")
    parser.add_argument("--dry-run", action="store_true", help="show what would run and what it would cost, spend nothing")
    parser.add_argument("--audio-only", action="store_true", help="re-record audio from the saved script")
    parser.add_argument("--new-script", action="store_true", help="rewrite the audio script (cheap) and re-record it (paid)")
    parser.add_argument("--force", action="store_true", help="rewrite the edition and re-record audio (costs money)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bootstrap()
    print(make_edition(date.fromisoformat(args.day) if args.day else None, args.edition, force=args.force,
                       audio_only=args.audio_only, new_script=args.new_script, dry_run=args.dry_run))
