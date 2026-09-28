"""Daily summary: gathers a day's stories, reads full articles, writes a capped summary, makes audio."""
import httpx


def test_daily_summary_end_to_end(db, tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app import digest
    from app.config import settings
    from app.db import conn
    from app.pipeline.fake_llm import FakeLLM

    monkeypatch.setattr(settings, "podcast_tts", "fake")
    monkeypatch.setattr(settings, "media_dir", tmp_path)
    article = "<html><body><article><p>" + "The full story in detail. " * 40 + "</p></article></body></html>"
    monkeypatch.setattr("app.sources.fetch.make_client",
                        lambda transport=None: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=article))))
    tz = ZoneInfo(settings.timezone)
    day = datetime(2026, 3, 10, tzinfo=tz).date()
    noon = datetime(2026, 3, 10, 12, 0, tzinfo=tz)
    with conn() as c:
        for i in range(3):
            c.execute("""insert into posts (url, canonical_url, title, summary, details, source_name, importance, feed_at)
                         values (%s, %s, %s, 'A summary.', 'Some details.', 'Wire', %s, %s)""",
                      (f"https://d.example/{i}", f"https://d.example/{i}", f"Story {i}", 3 - i, noon))
    stories = digest.stories_for_day(day)
    assert len(stories) == 3 and stories[0]["title"] == "Story 0"

    plan = digest.make_digest(day, FakeLLM(), dry_run=True)
    assert plan["dry_run"] and plan["summary"] == "write" and plan["stories"] == 3

    result = digest.make_digest(day, FakeLLM())
    assert result["audio"] == "ok" and result["words"] > 0, result
    assert result["full_text"] >= 1, "top stories should have their article text"

    # Running the same day again does nothing and calls nothing.
    llm = FakeLLM()
    again = digest.make_digest(day, llm)
    assert "already done" in again["skipped"] and llm.usage["calls"] == 0

    # Re-recording audio reuses the saved script: no Claude calls at all.
    llm = FakeLLM()
    redo = digest.make_digest(day, llm, audio_only=True)
    assert redo["audio"] == "ok" and redo["script"] == "reuse saved" and llm.usage["calls"] == 0

    # --new-script rewrites only the script (one cheap call) and re-records.
    llm = FakeLLM()
    fresh = digest.make_digest(day, llm, new_script=True)
    assert fresh["audio"] == "ok" and fresh["summary"] == "keep" and llm.usage["calls"] == 1

    # --force redoes everything, on the same row.
    llm = FakeLLM()
    forced = digest.make_digest(day, llm, force=True)
    assert forced["id"] == result["id"] and llm.usage["calls"] == 2  # summary + script

    from fastapi.testclient import TestClient
    from app.web import app

    with TestClient(app) as client:
        days = client.get("/api/digests").json()
        mine = [d for d in days if d["day"] == str(day)][0]
        assert mine["headline"] and mine["paragraphs"][0]["post_ids"]
        assert mine["sources"] and mine["audio_url"].endswith(".mp3")
        assert client.get(mine["audio_url"]).headers["content-type"] == "audio/mpeg"
        import feedparser

        feed = feedparser.parse(client.get("/podcast.xml").content)
        assert not feed.bozo and feed.entries
        assert client.post("/api/digests/not-a-date").status_code == 422
    with conn() as c:
        c.execute("delete from posts where url like 'https://d.example/%%'")
        c.execute("delete from episodes where day = %s", (day,))


def test_paid_voices_stop_at_the_monthly_limit(db, monkeypatch):
    import pytest

    from app import podcast
    from app.config import settings

    monkeypatch.setattr(settings, "podcast_tts", "elevenlabs")
    monkeypatch.setattr(settings, "podcast_monthly_char_limit", 100)
    with pytest.raises(RuntimeError, match="MONTHLY"):
        podcast.check_budget([{"speaker": "Alex", "text": "x" * 150}])
    monkeypatch.setattr(settings, "podcast_monthly_char_limit", 100000)
    monkeypatch.setattr(settings, "podcast_max_chars_per_episode", 50)
    with pytest.raises(RuntimeError, match="PER_EPISODE"):
        podcast.check_budget([{"speaker": "Alex", "text": "x" * 60}])
    monkeypatch.setattr(settings, "podcast_tts", "edge")
    podcast.check_budget([{"speaker": "Alex", "text": "x" * 100000}])  # free voices aren't capped


def test_elevenlabs_request_shape(monkeypatch):
    from app import podcast
    from app.config import settings

    monkeypatch.setattr(settings, "podcast_tts", "elevenlabs")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "k")
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, content=b"\\xff\\xfbMP3")

    audio = podcast.synthesize([{"speaker": settings.podcast_host_a, "text": "Hello"},
                                {"speaker": settings.podcast_host_b, "text": "Hi"}],
                               httpx.Client(transport=httpx.MockTransport(handler)))
    assert audio.count(b"MP3") == 2
    assert seen[0].headers["xi-api-key"] == "k"
    assert seen[0].url.path.endswith(settings.elevenlabs_voice_a) and seen[1].url.path.endswith(settings.elevenlabs_voice_b)
    assert b'"model_id"' in seen[0].content
