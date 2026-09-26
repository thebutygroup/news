"""Daily audio briefing: the day's top stories, read by two voices, published as a podcast feed.

    python -m app.podcast          make an episode now

1. Pick the top stories posted since the last episode: one post per story, ranked by
   importance, Bauer relevance and votes.
2. Claude writes a two-host script from our own summaries. It is told never to add facts.
3. Text to speech turns each turn into MP3, and the pieces are joined.
4. The MP3 goes in MEDIA_DIR, a Docker volume. It is served at /podcast/<id>.mp3, listed in the
   RSS feed at /podcast.xml, and shown as a player at the top of the feed.

Text to speech (PODCAST_TTS):
  edge   Microsoft Edge's read-aloud voices through the open-source edge-tts library. Free, no key.
         It uses an unofficial endpoint, so it can break or be switched off without notice.
  azure  The same neural voices through Azure's official Speech service. Needs AZURE_SPEECH_KEY.
  fake   Silence-free placeholder bytes, for tests.
"""
from __future__ import annotations

import json
import logging
import string
from datetime import timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

from .config import load_topics, settings
from .db import conn
from .normalize import utcnow

log = logging.getLogger("news.podcast")
PROMPT = Path(__file__).resolve().parent / "pipeline" / "prompts" / "podcast.md"
MP3_BYTES_PER_SECOND = 6000  # audio-24khz-48kbitrate-mono-mp3

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"speaker": {"type": "string"}, "text": {"type": "string"}},
                "required": ["speaker", "text"],
            },
        },
    },
    "required": ["title", "segments"],
}


# -- 1. pick stories ----------------------------------------------------------
def pick_stories(limit: int | None = None) -> list[dict]:
    with conn() as c:
        last = c.execute("select max(created_at) as t from episodes where status = 'ok'").fetchone()["t"]
        since = last or (utcnow() - timedelta(hours=24))
        return c.execute(
            """select * from (
                 select distinct on (coalesce(p.story_id, -p.id)) p.id, p.title, p.summary, p.delta, p.url,
                        p.source_name, p.importance, p.lens_score, p.feed_at,
                        (select count(*) from coverage cv where cv.story_id = p.story_id) as outlets,
                        (select count(*) from votes v where v.post_id = p.id) as votes
                 from posts p
                 where not p.hidden and p.posted_at > %s
                   and not exists (select 1 from feedback f where f.post_id = p.id)
                 order by coalesce(p.story_id, -p.id), p.importance desc, p.feed_at desc
               ) s
               order by importance * 3 + lens_score + votes desc, feed_at desc
               limit %s""",
            (since, limit or settings.podcast_stories),
        ).fetchall()


# -- 2. script ----------------------------------------------------------------
def write_script(llm, stories: list[dict]) -> dict:
    topics = load_topics()
    lens = topics[0].lens.strip() if topics else ""
    today = utcnow().strftime("%A %d %B %Y")
    system = string.Template(PROMPT.read_text()).safe_substitute(
        host_a=settings.podcast_host_a, host_b=settings.podcast_host_b, lens=lens,
        words=str(settings.podcast_words), today=today)
    result = llm.structured(
        model=settings.podcast_model, system=system, tool_name="podcast_script", schema=SCRIPT_SCHEMA,
        payload={"today": today, "hosts": [settings.podcast_host_a, settings.podcast_host_b],
                 "stories": [{"headline": s["title"], "source": s["source_name"], "summary": s["summary"],
                              "new": s["delta"], "other_outlets": s["outlets"], "importance": s["importance"]}
                             for s in stories]},
    )
    hosts = {settings.podcast_host_a, settings.podcast_host_b}
    segments = [{"speaker": seg["speaker"] if seg.get("speaker") in hosts else settings.podcast_host_a,
                 "text": seg["text"].strip()} for seg in result.get("segments", []) if seg.get("text", "").strip()]
    if not segments:
        raise RuntimeError("the script came back empty")
    return {"title": (result.get("title") or f"Briefing for {today}").strip()[:120], "segments": segments}


# -- 3. text to speech --------------------------------------------------------
def _voice(speaker: str) -> str:
    return settings.podcast_voice_b if speaker == settings.podcast_host_b else settings.podcast_voice_a


def _edge(text: str, voice: str) -> bytes:
    import edge_tts

    audio = bytearray()
    for chunk in edge_tts.Communicate(text, voice).stream_sync():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    return bytes(audio)


def _azure(text: str, voice: str, client: httpx.Client) -> bytes:
    if not settings.azure_speech_key:
        raise RuntimeError("PODCAST_TTS=azure needs AZURE_SPEECH_KEY")
    ssml = (f"<speak version='1.0' xml:lang='en-GB'><voice name='{escape(voice)}'>{escape(text)}</voice></speak>")
    r = client.post(
        f"https://{settings.azure_speech_region}.tts.speech.microsoft.com/cognitiveservices/v1",
        content=ssml.encode(),
        headers={"Ocp-Apim-Subscription-Key": settings.azure_speech_key, "Content-Type": "application/ssml+xml",
                 "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3", "User-Agent": "news-briefing"},
    )
    r.raise_for_status()
    return r.content


def synthesize(segments: list[dict]) -> bytes:
    """One MP3 per turn, joined end to end. MP3 is a stream of frames, so same-format files concatenate cleanly."""
    mode = settings.podcast_tts
    parts: list[bytes] = []
    with httpx.Client(timeout=60) as client:
        for seg in segments:
            if mode == "fake":
                parts.append(b"\xff\xfb" + seg["text"].encode()[:60])
            elif mode == "azure":
                parts.append(_azure(seg["text"], _voice(seg["speaker"]), client))
            else:
                parts.append(_edge(seg["text"], _voice(seg["speaker"])))
    return b"".join(parts)


# -- 4. publish ---------------------------------------------------------------
def make_episode(llm=None) -> dict:
    from .pipeline.llm import get_llm

    stories = pick_stories()
    if not stories:
        log.info("no new stories since the last episode, skipping")
        return {"skipped": "no new stories"}
    llm = llm or get_llm()
    notes = "\n".join(f"{i}. {s['title']} ({s['source_name'] or 'source'}): {s['url']}" for i, s in enumerate(stories, 1))
    with conn() as c:
        ep_id = c.execute("insert into episodes (title, script, post_ids, notes, status) values ('(in progress)', '[]', %s, %s, 'error') returning id",
                          ([s["id"] for s in stories], notes)).fetchone()["id"]
    try:
        script = write_script(llm, stories)
        audio = synthesize(script["segments"])
        settings.media_dir.mkdir(parents=True, exist_ok=True)
        name = f"briefing-{ep_id}.mp3"
        (settings.media_dir / name).write_bytes(audio)
        with conn() as c:
            c.execute("""update episodes set title = %s, script = %s, audio_file = %s, bytes = %s, duration_seconds = %s,
                         status = 'ok', error = null where id = %s""",
                      (script["title"], json.dumps(script["segments"]), name, len(audio),
                       len(audio) // MP3_BYTES_PER_SECOND, ep_id))
        prune()
        log.info("episode %s: %s (%d stories, %d KB)", ep_id, script["title"], len(stories), len(audio) // 1024)
        return {"id": ep_id, "title": script["title"], "stories": len(stories), "bytes": len(audio)}
    except Exception as exc:  # noqa: BLE001 - record the failure, keep the worker alive
        log.exception("episode failed")
        with conn() as c:
            c.execute("update episodes set error = %s where id = %s", (str(exc)[:1000], ep_id))
        return {"id": ep_id, "error": str(exc)}


def prune() -> None:
    """Keep the newest PODCAST_KEEP episodes' audio; older rows stay, their files go."""
    with conn() as c:
        old = c.execute("""select id, audio_file from episodes where audio_file is not null and id not in
                           (select id from episodes where audio_file is not null order by id desc limit %s)""",
                        (settings.podcast_keep,)).fetchall()
        for row in old:
            (settings.media_dir / row["audio_file"]).unlink(missing_ok=True)
            c.execute("update episodes set audio_file = null where id = %s", (row["id"],))


def latest() -> dict | None:
    with conn() as c:
        return c.execute("""select id, title, created_at, duration_seconds, bytes from episodes
                            where status = 'ok' and audio_file is not null order by id desc limit 1""").fetchone()


def rss() -> str:
    base = settings.public_base_url
    with conn() as c:
        eps = c.execute("""select * from episodes where status = 'ok' and audio_file is not null
                           order by id desc limit 50""").fetchall()
    items = []
    for e in eps:
        mins, secs = divmod(e["duration_seconds"] or 0, 60)
        items.append(f"""    <item>
      <title>{escape(e['title'])}</title>
      <description>{escape(e['notes'])}</description>
      <enclosure url="{base}/podcast/{e['id']}.mp3" length="{e['bytes'] or 0}" type="audio/mpeg"/>
      <guid isPermaLink="false">news-briefing-{e['id']}</guid>
      <pubDate>{format_datetime(e['created_at'].astimezone(timezone.utc))}</pubDate>
      <itunes:duration>{mins}:{secs:02d}</itunes:duration>
    </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>News daily briefing</title>
    <link>{base}/</link>
    <description>The day's top AI stories from {escape(base)}, read aloud.</description>
    <language>en-gb</language>
    <itunes:author>News</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="Technology"/>
{chr(10).join(items)}
  </channel>
</rss>
"""


if __name__ == "__main__":
    from .seed import bootstrap

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bootstrap()
    print(make_episode())
