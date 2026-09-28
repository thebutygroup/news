"""Daily summary: one short, factual write-up of everything important published on a day.

    python -m app.digest              summarise yesterday (UK time)
    python -m app.digest 2026-09-24   summarise a given day (replaces any existing summary)

Runs at 05:30 UK (DIGEST_CRON), after the 04:00 scan has caught the US evening's news, and
summarises the previous day. Steps:
1. Gather every post filed on that day, one entry per story, most important first.
2. For the top DIGEST_FULLTEXT_STORIES stories, fetch the article and extract its text, so the
   summary is written from the full story and not just our two-line version. The text is used for
   that one call and never stored.
3. Claude (DIGEST_MODEL, Sonnet by default) writes the summary: factual, capped at
   DIGEST_MAX_WORDS, one mention per story, skipping anything already covered on previous days
   unless there's major news. Each paragraph lists the posts it draws on.
4. If PODCAST_ENABLED, the audio version is made from it (app/podcast.py).
No Bauer lens here: the summary is for anyone.
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

log = logging.getLogger("news.digest")
PROMPT = Path(__file__).resolve().parent / "pipeline" / "prompts" / "digest.md"

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


def day_label(day: date) -> str:
    return day.strftime("%A %d %B %Y").replace(" 0", " ")


def yesterday() -> date:
    return (datetime.now(ZoneInfo(settings.timezone)) - timedelta(days=1)).date()


def stories_for_day(day: date) -> list[dict]:
    tz = ZoneInfo(settings.timezone)
    start = datetime.combine(day, time.min, tz)
    end = start + timedelta(days=1)
    with conn() as c:
        return c.execute(
            """select * from (
                 select distinct on (coalesce(p.story_id, -p.id)) p.id, p.story_id, p.title, p.summary, p.details,
                        p.url, p.source_name, p.importance, p.feed_at,
                        (select string_agg(p2.delta, ' ') from posts p2 where p2.story_id = p.story_id
                           and p2.delta is not null and p2.feed_at >= %(s)s and p2.feed_at < %(e)s) as new_today,
                        (select count(*) from coverage cv where cv.story_id = p.story_id) as outlets,
                        (select count(*) from votes v where v.post_id = p.id) as votes
                 from posts p
                 where not p.hidden and p.feed_at >= %(s)s and p.feed_at < %(e)s
                   and not exists (select 1 from feedback f where f.post_id = p.id)
                 order by coalesce(p.story_id, -p.id), p.feed_at
               ) s
               order by importance * 3 + least(outlets, 5) + votes desc, feed_at""",
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


def previous_summaries(day: date, days: int = 2) -> list[dict]:
    with conn() as c:
        rows = c.execute("""select day, headline, summary from episodes where day < %s and day >= %s
                            and status = 'ok' order by day desc""", (day, day - timedelta(days=days))).fetchall()
    return [{"day": str(r["day"]), "headline": r["headline"], "text": " ".join(p["text"] for p in r["summary"] or [])}
            for r in rows]


def write_digest(llm, day: date, stories: list[dict]) -> dict:
    system = string.Template(PROMPT.read_text()).safe_substitute(
        day=day_label(day), words=str(settings.digest_words), max_words=str(settings.digest_max_words))
    result = llm.structured(
        model=settings.digest_model, system=system, tool_name="digest_summary", schema=SCHEMA, max_tokens=4000,
        payload={"day": day_label(day), "previous_days": previous_summaries(day),
                 "stories": [{k: v for k, v in {
                     "id": s["id"], "headline": s["title"], "source": s["source_name"], "summary": s["summary"],
                     "details": s["details"], "new_today": s["new_today"], "other_outlets": s["outlets"],
                     "importance": s["importance"], "full_text": s.get("full_text")}.items() if v not in (None, "")}
                     for s in stories]},
    )
    valid = {s["id"] for s in stories}
    paragraphs = [{"text": p["text"].strip(), "post_ids": [i for i in p.get("post_ids", []) if i in valid]}
                  for p in result.get("paragraphs", []) if p.get("text", "").strip()]
    if not paragraphs:
        raise RuntimeError("the summary came back empty")
    return {"headline": (result.get("headline") or f"AI news, {day_label(day)}").strip()[:160], "paragraphs": paragraphs}


def existing(day: date) -> dict | None:
    with conn() as c:
        return c.execute("select * from episodes where day = %s", (day,)).fetchone()


def make_digest(day: date | None = None, llm=None, audio: bool | None = None, force: bool = False,
                audio_only: bool = False, dry_run: bool = False, new_script: bool = False) -> dict:
    """Make a day's summary and audio, doing only what's missing.

    Default: a day that already has a summary keeps it (no Claude call). If its audio is missing, the
    audio is made from the saved script when there is one, so only text to speech is paid for. A day
    that's fully done is left alone. force=True redoes everything; audio_only=True re-records audio
    from the saved script; dry_run=True reports what would happen and spends nothing.
    """
    from .pipeline.llm import get_llm

    audio_only = audio_only or new_script
    day = day or yesterday()
    want_audio = settings.podcast_enabled if audio is None else audio
    row = existing(day)
    # A summary written before its day was over (say, a manual run at lunchtime) is provisional.
    # Once the day has ended, the next run rewrites it with the whole day, once.
    day_end = datetime.combine(day, time.min, ZoneInfo(settings.timezone)) + timedelta(days=1)
    provisional = bool(row and row["created_at"] < day_end and datetime.now(ZoneInfo(settings.timezone)) >= day_end)
    have_summary = bool(row and row["status"] == "ok" and row["summary"]) and not provisional
    have_audio = bool(row and row["audio_file"])
    have_script = bool(row and row["script"])
    redo_summary = force or not have_summary
    redo_audio = want_audio and (force or audio_only or redo_summary or not have_audio)  # new summary, new audio
    if audio_only:
        redo_summary = not have_summary

    plan = {"day": str(day),
            "summary": ("rewrite: it was written before the day was over" if redo_summary and provisional
                        else "write" if redo_summary else "keep"),
            "audio": ("record" if redo_audio else "keep" if have_audio else "off"),
            "script": ("reuse saved" if redo_audio and have_script and not (force or new_script or redo_summary) else
                       "write" if redo_audio else "-")}
    if dry_run:
        stories = stories_for_day(day)
        plan.update({"stories": len(stories), "full_text_candidates": min(len(stories), settings.digest_fulltext_stories),
                     "tts": settings.podcast_tts,
                     "tts_chars_estimate": (script_chars_saved(row) if plan["script"] == "reuse saved"
                                            else settings.podcast_words * 6) if redo_audio else 0,
                     "tts_chars_this_month": _chars_this_month()})
        return {"dry_run": True, **plan}
    if not redo_summary and not redo_audio:
        note = ("already done; pass --force to redo it (that costs a Claude call and audio)" if row["created_at"] >= day_end
                else "written today, so it's provisional; it will be rewritten with the whole day after midnight")
        return {**plan, "skipped": note}

    llm = llm or get_llm()
    result = {**plan}
    if redo_summary:
        stories = stories_for_day(day)
        if not stories:
            log.info("no posts on %s, no summary", day)
            return {"day": str(day), "skipped": "no posts that day"}
        fetch_full_text(stories)
        try:
            digest = write_digest(llm, day, stories)
        except Exception as exc:  # noqa: BLE001
            log.exception("summary failed")
            return {"day": str(day), "error": str(exc)}
        cited = {i for p in digest["paragraphs"] for i in p["post_ids"]}
        sources = [{"id": s["id"], "title": s["title"], "url": s["url"], "source": s["source_name"]}
                   for s in stories if s["id"] in cited]
        notes = "\n".join(f"{s['title']} ({s['source'] or 'source'}): {s['url']}" for s in sources)
        with conn() as c:
            row = c.execute(
                """insert into episodes (day, title, headline, summary, summary_model, sources, script, post_ids, notes, status)
                   values (%s, %s, %s, %s, %s, %s, '[]', %s, %s, 'ok')
                   on conflict (day) where day is not null do update set title = excluded.title, headline = excluded.headline,
                     summary = excluded.summary, summary_model = excluded.summary_model, sources = excluded.sources,
                     script = '[]', post_ids = excluded.post_ids, notes = excluded.notes, status = 'ok', error = null,
                     created_at = now()
                   returning *""",
                (day, digest["headline"], digest["headline"], json.dumps(digest["paragraphs"]), settings.digest_model,
                 json.dumps(sources), [s["id"] for s in stories], notes),
            ).fetchone()
        result.update({"headline": digest["headline"], "stories": len(stories),
                       "full_text": sum(1 for s in stories if s.get("full_text")),
                       "words": sum(len(p["text"].split()) for p in digest["paragraphs"])})
    else:
        digest = {"headline": row["headline"], "paragraphs": row["summary"]}
        stories = stories_for_day(day)
    result["id"] = row["id"]

    if redo_audio:
        from . import podcast

        try:
            segments = row["script"] if (row["script"] and not (force or new_script or redo_summary)) else None
            if not segments:
                segments = podcast.write_script(llm, day_label(day), digest, stories)
                with conn() as c:
                    c.execute("update episodes set script = %s where id = %s", (json.dumps(segments), row["id"]))
            podcast.check_budget(segments)
            chars = podcast.script_chars(segments)
            podcast.save_audio(row["id"], podcast.synthesize(segments), chars)
            result.update({"audio": "ok", "tts_chars": chars})
        except Exception as exc:  # noqa: BLE001 - the written summary stands even if audio fails
            log.exception("audio failed")
            with conn() as c:
                c.execute("update episodes set audio_error = %s where id = %s", (str(exc)[:1000], row["id"]))
            result["audio"] = f"error: {exc}"
    result["llm"] = getattr(llm, "usage", {})
    log.info("summary for %s: %s", day, result)
    return result


def script_chars_saved(row) -> int:
    return sum(len(s["text"]) for s in (row["script"] or [])) if row else 0


def _chars_this_month() -> int:
    from .podcast import chars_this_month

    return chars_this_month()


def recent(days: int = 60) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """select id, day, headline, summary, sources, duration_seconds, audio_file from episodes
               where day is not null and status = 'ok' order by day desc limit %s""", (days,)).fetchall()
    return [{"day": str(r["day"]), "headline": r["headline"], "paragraphs": r["summary"], "sources": r["sources"] or [],
             "audio_url": f"/podcast/{r['id']}.mp3" if r["audio_file"] else None,
             "duration_seconds": r["duration_seconds"]} for r in rows]


if __name__ == "__main__":
    import argparse

    from .seed import bootstrap

    parser = argparse.ArgumentParser(description="Write a day's summary and audio. Only does what's missing.")
    parser.add_argument("day", nargs="?", help="YYYY-MM-DD, default yesterday (UK time)")
    parser.add_argument("--dry-run", action="store_true", help="show what would run and what it would cost, spend nothing")
    parser.add_argument("--audio-only", action="store_true", help="re-record audio from the saved script, keep the summary")
    parser.add_argument("--new-script", action="store_true",
                        help="rewrite the audio script (cheap) and re-record it (paid), keep the summary")
    parser.add_argument("--force", action="store_true", help="rewrite the summary and re-record audio (costs money)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bootstrap()
    print(make_digest(date.fromisoformat(args.day) if args.day else None, force=args.force,
                      audio_only=args.audio_only, dry_run=args.dry_run, new_script=args.new_script))
