"""Twice-daily editions: each covers only what's new since the one before, and never runs twice by accident."""
from datetime import datetime, timedelta, timezone

import httpx


def _post(c, n, when):
    c.execute("""insert into posts (url, canonical_url, title, summary, details, source_name, importance, feed_at, posted_at)
                 values (%s, %s, %s, 'A summary.', 'Some details.', 'Wire', 3, %s, %s)""",
              (f"https://e.example/{n}", f"https://e.example/{n}", f"Story {n}", when, when))


def test_editions_cover_only_whats_new(db, tmp_path, monkeypatch):
    from app import digest
    from app.config import settings
    from app.db import conn
    from app.pipeline.fake_llm import FakeLLM

    monkeypatch.setattr(settings, "podcast_tts", "fake")
    monkeypatch.setattr(settings, "media_dir", tmp_path)
    monkeypatch.setattr("app.digest.fetch_full_text", lambda stories: None)
    T = datetime(2026, 3, 20, 5, 0, tzinfo=timezone.utc)
    day = T.date()
    with conn() as c:
        c.execute("delete from episodes")
        _post(c, "a", T - timedelta(hours=2))
        _post(c, "b", T - timedelta(hours=3))

    monkeypatch.setattr("app.digest.utcnow", lambda: T)
    assert digest.make_edition(day, "morning", FakeLLM(), dry_run=True)["stories"] == 2
    morning = digest.make_edition(day, "morning", FakeLLM())
    assert morning["stories"] == 2 and morning["audio"] == "ok", morning

    with conn() as c:
        _post(c, "c", T + timedelta(hours=5))
    monkeypatch.setattr("app.digest.utcnow", lambda: T + timedelta(hours=7))
    afternoon = digest.make_edition(day, "afternoon", FakeLLM())
    assert afternoon["stories"] == 1, "the afternoon edition only sees what came after the morning one"
    with conn() as c:
        rows = c.execute("select edition, post_ids from episodes where day = %s order by window_end", (day,)).fetchall()
    assert set(rows[0]["post_ids"]).isdisjoint(rows[1]["post_ids"])

    # Running it again does nothing; forcing it keeps the same window, so the same stories.
    llm = FakeLLM()
    assert "already done" in digest.make_edition(day, "afternoon", llm)["skipped"] and llm.usage["calls"] == 0
    forced = digest.make_edition(day, "afternoon", FakeLLM(), force=True)
    assert forced["stories"] == 1 and forced["window"] == afternoon["window"]

    # Nothing new since the last edition: no edition, no spend.
    monkeypatch.setattr("app.digest.utcnow", lambda: T + timedelta(hours=23))
    llm = FakeLLM()
    assert "nothing new" in digest.make_edition(day + timedelta(days=1), "morning", llm)["skipped"]
    assert llm.usage["calls"] == 0

    editions = [d for d in digest.recent() if d["day"] == str(day)]
    assert sorted(d["edition"] for d in editions) == ["afternoon", "morning"]
    assert all(d["audio_url"] and d["covers_until"] for d in editions)

    from fastapi.testclient import TestClient
    from app.web import app

    with TestClient(app) as client:
        assert len([d for d in client.get("/api/digests").json() if d["day"] == str(day)]) == 2
        assert client.post(f"/api/digests/{day}/evening").status_code == 422
        import feedparser

        feed = feedparser.parse(client.get("/podcast.xml").content)
        assert not feed.bozo and any(e.title.startswith("Afternoon:") for e in feed.entries)
    with conn() as c:
        c.execute("delete from posts where url like 'https://e.example/%%'")
        c.execute("delete from episodes")


def test_editions_made_late_still_split_at_noon(db, tmp_path, monkeypatch):
    from app import digest
    from app.config import settings
    from app.db import conn
    from app.pipeline.fake_llm import FakeLLM

    monkeypatch.setattr(settings, "podcast_enabled", False)
    monkeypatch.setattr("app.digest.fetch_full_text", lambda stories: None)
    # 20 March 2026: the UK is on GMT, so local time equals UTC here.
    day = datetime(2026, 3, 20).date()
    noon = datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc)
    with conn() as c:
        c.execute("delete from episodes")
        _post(c, "night", noon - timedelta(hours=9))     # 03:00, overnight
        _post(c, "lunch", noon + timedelta(hours=2))     # 14:00, afternoon
    monkeypatch.setattr("app.digest.utcnow", lambda: noon + timedelta(hours=6))  # both made at 18:00
    morning = digest.make_edition(day, "morning", FakeLLM())
    afternoon = digest.make_edition(day, "afternoon", FakeLLM())
    assert morning["stories"] == 1 and afternoon["stories"] == 1
    assert morning["window"][1].startswith("2026-03-20T12:00")
    with conn() as c:
        c.execute("delete from posts where url like 'https://e.example/%%'")
        c.execute("delete from episodes")


def test_only_scheduled_scans_make_editions(db, monkeypatch):
    from app.config import settings
    from app.db import conn
    from app.pipeline.run import run_scan

    monkeypatch.setattr(settings, "digest_enabled", True)
    monkeypatch.setattr(settings, "podcast_enabled", False)
    monkeypatch.setattr("app.digest.fetch_full_text", lambda stories: None)
    manual = run_scan("manual:joe@example.com", force=True)
    assert "edition" not in manual["stats"]
    with conn() as c:
        _post(c, "sched", datetime.now(timezone.utc))
    scheduled = run_scan("schedule")
    assert scheduled["stats"]["edition"]["edition"] in ("morning", "afternoon")
    with conn() as c:
        c.execute("delete from posts where url = 'https://e.example/sched'")
        c.execute("delete from episodes")


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
