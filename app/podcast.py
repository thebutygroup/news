"""The audio version of each day's summary.

Claude (PODCAST_MODEL, Haiku by default) turns the written summary into a two-host script, text to
speech reads it, and the MP3 goes in MEDIA_DIR (a Docker volume, never git). It plays from the day's
banner on the site, and /podcast.xml is a feed any podcast app can subscribe to.

Text to speech (PODCAST_TTS):
  edge        Microsoft Edge's neural voices via the open-source edge-tts library. Free, no key, but an
              unofficial endpoint that can break without notice.
  azure       The same voices through Azure's official Speech service. Needs AZURE_SPEECH_KEY.
  elevenlabs  The most natural voices. Needs ELEVENLABS_API_KEY; pay per character.
  fake        Placeholder bytes, for tests.
"""
from __future__ import annotations

import logging
import string
from datetime import timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

from .config import settings
from .db import conn

log = logging.getLogger("news.podcast")
PROMPT = Path(__file__).resolve().parent / "pipeline" / "prompts" / "podcast.md"
BYTES_PER_SECOND = {"edge": 6000, "azure": 6000, "elevenlabs": 16000, "fake": 6000}

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"speaker": {"type": "string"}, "text": {"type": "string"}},
                "required": ["speaker", "text"],
            },
        },
    },
    "required": ["segments"],
}


def write_script(llm, day_label: str, digest: dict, stories: list[dict]) -> list[dict]:
    system = string.Template(PROMPT.read_text()).safe_substitute(
        host_a=settings.podcast_host_a, host_b=settings.podcast_host_b, words=str(settings.podcast_words), day=day_label)
    cited = {i for p in digest["paragraphs"] for i in p.get("post_ids", [])}
    result = llm.structured(
        model=settings.podcast_model, system=system, tool_name="podcast_script", schema=SCRIPT_SCHEMA,
        payload={"day": day_label, "hosts": [settings.podcast_host_a, settings.podcast_host_b],
                 "headline": digest["headline"], "summary": [p["text"] for p in digest["paragraphs"]],
                 "stories": [{"headline": s["title"], "source": s["source_name"], "summary": s["summary"],
                              "details": s["details"]} for s in stories if s["id"] in cited]},
    )
    hosts = {settings.podcast_host_a, settings.podcast_host_b}
    segments = [{"speaker": seg["speaker"] if seg.get("speaker") in hosts else settings.podcast_host_a,
                 "text": seg["text"].strip()} for seg in result.get("segments", []) if seg.get("text", "").strip()]
    if not segments:
        raise RuntimeError("the script came back empty")
    return segments


# -- text to speech -------------------------------------------------------------
def _voice(speaker: str) -> str:
    b = speaker == settings.podcast_host_b
    if settings.podcast_tts == "elevenlabs":
        return settings.elevenlabs_voice_b if b else settings.elevenlabs_voice_a
    return settings.podcast_voice_b if b else settings.podcast_voice_a


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
    ssml = f"<speak version='1.0' xml:lang='en-GB'><voice name='{escape(voice)}'>{escape(text)}</voice></speak>"
    r = client.post(
        f"https://{settings.azure_speech_region}.tts.speech.microsoft.com/cognitiveservices/v1",
        content=ssml.encode(),
        headers={"Ocp-Apim-Subscription-Key": settings.azure_speech_key, "Content-Type": "application/ssml+xml",
                 "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3", "User-Agent": "news-briefing"},
    )
    r.raise_for_status()
    return r.content


def _elevenlabs(text: str, voice: str, client: httpx.Client) -> bytes:
    if not settings.elevenlabs_api_key:
        raise RuntimeError("PODCAST_TTS=elevenlabs needs ELEVENLABS_API_KEY")
    r = client.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
        params={"output_format": "mp3_44100_128"},
        headers={"xi-api-key": settings.elevenlabs_api_key, "Accept": "audio/mpeg"},
        json={"text": text, "model_id": settings.elevenlabs_model},
    )
    if r.status_code >= 400:
        raise RuntimeError(f"ElevenLabs {r.status_code}: {r.text[:300]}")
    return r.content


def synthesize(segments: list[dict], client: httpx.Client | None = None) -> bytes:
    """One MP3 per turn, joined end to end. Same-format MP3s are frame streams, so they concatenate cleanly."""
    mode = settings.podcast_tts
    own = client is None
    client = client or httpx.Client(timeout=120)
    try:
        parts = []
        for seg in segments:
            if mode == "fake":
                parts.append(b"\xff\xfb" + seg["text"].encode()[:60])
            elif mode == "azure":
                parts.append(_azure(seg["text"], _voice(seg["speaker"]), client))
            elif mode == "elevenlabs":
                parts.append(_elevenlabs(seg["text"], _voice(seg["speaker"]), client))
            else:
                parts.append(_edge(seg["text"], _voice(seg["speaker"])))
        return b"".join(parts)
    finally:
        if own:
            client.close()


def save_audio(episode_id: int, audio: bytes) -> None:
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    name = f"briefing-{episode_id}.mp3"
    (settings.media_dir / name).write_bytes(audio)
    seconds = len(audio) // BYTES_PER_SECOND.get(settings.podcast_tts, 6000)
    with conn() as c:
        c.execute("update episodes set audio_file = %s, bytes = %s, duration_seconds = %s, audio_error = null where id = %s",
                  (name, len(audio), seconds, episode_id))
    prune()


def prune() -> None:
    """Keep the newest PODCAST_KEEP episodes' audio. Written summaries are kept forever."""
    with conn() as c:
        old = c.execute("""select id, audio_file from episodes where audio_file is not null and id not in
                           (select id from episodes where audio_file is not null order by id desc limit %s)""",
                        (settings.podcast_keep,)).fetchall()
        for row in old:
            (settings.media_dir / row["audio_file"]).unlink(missing_ok=True)
            c.execute("update episodes set audio_file = null where id = %s", (row["id"],))


def rss() -> str:
    base = settings.public_base_url
    with conn() as c:
        eps = c.execute("""select * from episodes where status = 'ok' and audio_file is not null
                           order by coalesce(day, created_at::date) desc, id desc limit 50""").fetchall()
    items = []
    for e in eps:
        mins, secs = divmod(e["duration_seconds"] or 0, 60)
        items.append(f"""    <item>
      <title>{escape(e['headline'] or e['title'])}</title>
      <description>{escape(e['notes'])}</description>
      <enclosure url="{base}/podcast/{e['id']}.mp3" length="{e['bytes'] or 0}" type="audio/mpeg"/>
      <guid isPermaLink="false">news-briefing-{e['id']}</guid>
      <pubDate>{format_datetime(e['created_at'].astimezone(timezone.utc))}</pubDate>
      <itunes:duration>{mins}:{secs:02d}</itunes:duration>
    </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>News daily summary</title>
    <link>{base}/</link>
    <description>Each day's AI news from {escape(base)}, summarised and read aloud.</description>
    <language>en-gb</language>
    <itunes:author>News</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="Technology"/>
{chr(10).join(items)}
  </channel>
</rss>
"""
