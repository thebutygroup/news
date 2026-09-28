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
import sys
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


def make_digest(day: date | None = None, llm=None, audio: bool | None = None) -> dict:
    from .pipeline.llm import get_llm

    day = day or yesterday()
    stories = stories_for_day(day)
    if not stories:
        log.info("no posts on %s, no summary", day)
        return {"day": str(day), "skipped": "no posts that day"}
    llm = llm or get_llm()
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
                 post_ids = excluded.post_ids, notes = excluded.notes, status = 'ok', error = null, created_at = now()
               returning id""",
            (day, digest["headline"], digest["headline"], json.dumps(digest["paragraphs"]), settings.digest_model,
             json.dumps(sources), [s["id"] for s in stories], notes),
        ).fetchone()
    result = {"day": str(day), "id": row["id"], "headline": digest["headline"], "stories": len(stories),
              "full_text": sum(1 for s in stories if s.get("full_text")),
              "words": sum(len(p["text"].split()) for p in digest["paragraphs"])}
    if settings.podcast_enabled if audio is None else audio:
        from . import podcast

        try:
            segments = podcast.write_script(llm, day_label(day), digest, stories)
            with conn() as c:
                c.execute("update episodes set script = %s where id = %s", (json.dumps(segments), row["id"]))
            podcast.save_audio(row["id"], podcast.synthesize(segments))
            result["audio"] = "ok"
        except Exception as exc:  # noqa: BLE001 - the written summary stands even if audio fails
            log.exception("audio failed")
            with conn() as c:
                c.execute("update episodes set audio_error = %s where id = %s", (str(exc)[:1000], row["id"]))
            result["audio"] = f"error: {exc}"
    log.info("summary for %s: %s", day, result)
    return result


def recent(days: int = 60) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """select id, day, headline, summary, sources, duration_seconds, audio_file from episodes
               where day is not null and status = 'ok' order by day desc limit %s""", (days,)).fetchall()
    return [{"day": str(r["day"]), "headline": r["headline"], "paragraphs": r["summary"], "sources": r["sources"] or [],
             "audio_url": f"/podcast/{r['id']}.mp3" if r["audio_file"] else None,
             "duration_seconds": r["duration_seconds"]} for r in rows]


if __name__ == "__main__":
    from .seed import bootstrap

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bootstrap()
    target = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else None
    print(make_digest(target))
