"""Daily briefing: picks stories, writes a script, makes audio, publishes a valid feed."""
import feedparser


def test_briefing_end_to_end(db, tmp_path, monkeypatch):
    from app.config import settings
    from app.pipeline.fake_llm import FakeLLM
    from app.podcast import make_episode, pick_stories

    monkeypatch.setattr(settings, "podcast_tts", "fake")
    monkeypatch.setattr(settings, "media_dir", tmp_path)
    assert pick_stories(), "the pipeline tests should have left posts to talk about"
    ep = make_episode(FakeLLM())
    assert ep.get("id") and not ep.get("error"), ep
    assert (tmp_path / f"briefing-{ep['id']}.mp3").exists()
    assert make_episode(FakeLLM()) == {"skipped": "no new stories"}  # nothing new since

    from fastapi.testclient import TestClient
    from app.web import app

    with TestClient(app) as client:
        latest = client.get("/api/podcast/latest").json()
        assert latest["id"] == ep["id"] and latest["url"].endswith(".mp3")
        audio = client.get(latest["url"])
        assert audio.status_code == 200 and audio.headers["content-type"] == "audio/mpeg"
        feed = feedparser.parse(client.get("/podcast.xml").content)
        assert not feed.bozo and feed.entries[0].enclosures[0].type == "audio/mpeg"
        assert client.get("/podcast/9999.mp3").status_code == 404
