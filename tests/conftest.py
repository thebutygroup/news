"""Tests run against a real Postgres. Point TEST_DATABASE_URL at a throwaway database;
every session drops and recreates its public schema."""
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = os.getenv("TEST_DATABASE_URL")
if not TEST_DB:
    collect_ignore = ["test_pipeline.py", "test_api.py", "test_sources.py"]  # database tests need TEST_DATABASE_URL

WORK = Path(os.getenv("TEST_WORKDIR", "/tmp/news-tests"))
os.environ.update({
    "DATABASE_URL": TEST_DB or "postgresql://unused",
    "LLM_FAKE": "1",
    "TOPICS_DIR": str(WORK / "topics"),
    "DEV_USER_EMAIL": "joe@example.com",
    "ADMIN_EMAILS": "joe@example.com",
    "STORY_TAG_THRESHOLD": "3",
    "REGISTRY_PATH": str(WORK / "registry.json"),
    "CHANNEL_DISCOVERY_PER_RUN": "0",
    "FETCH_WORKERS": "2",
})


def rss(path: Path, title: str, items: list[tuple[str, str, str]]) -> None:
    now = datetime.now(timezone.utc)
    entries = "".join(
        f"<item><title>{t}</title><link>{link}</link><description>{d}</description>"
        f"<pubDate>{format_datetime(now - timedelta(minutes=10 * i))}</pubDate></item>"
        for i, (t, link, d) in enumerate(items)
    )
    path.write_text(f'<?xml version="1.0"?><rss version="2.0"><channel><title>{title}</title>{entries}</channel></rss>')


@pytest.fixture(scope="session")
def workdir():
    if WORK.exists():
        shutil.rmtree(WORK)
    topic = WORK / "topics" / "ai"
    topic.mkdir(parents=True)
    real = ROOT / "topics" / "ai"
    for name in ("lens.md", "taxonomy.json"):
        shutil.copy(real / name, topic / name)
    meta = json.loads((real / "topic.json").read_text())
    meta["discovery_queries"] = []
    (topic / "topic.json").write_text(json.dumps(meta))
    (topic / "sources.json").write_text(json.dumps([
        {"name": "Tech Wire", "url": f"file://{WORK}/wire.xml", "tier": 2},
    ]))
    (WORK / "registry.json").write_text(json.dumps({"orgs": [
        {"slug": "acme-ai", "name": "Acme AI", "kind": "lab", "homepage": "https://acme.ai", "tier": 1,
         "aliases": ["Acme"], "topics": ["ai"],
         "channels": [{"url": f"file://{WORK}/lab.xml", "channel": "blog", "read": "rss"},
                      {"url": "https://x.com/AcmeAI", "channel": "x"}]},
    ]}))
    rss(WORK / "lab.xml", "Lab Blog", [
        ("Acme AI releases Nova-3 open weights model", "https://acme.ai/blog/nova-3", "Nova-3 ships with open weights."),
        ("Hugging Face confirms breach of Spaces secrets", "https://huggingface.co/blog/security-incident", "Tokens exposed."),
        ("Quarterly office party photos", "https://acme.ai/blog/party", "Cake and balloons."),
    ])
    rss(WORK / "wire.xml", "Tech Wire", [
        ("Hugging Face confirms breach of Spaces secrets tokens", "https://wire.example/hf-breach", "Rewrite of the post."),
        ("Acme AI releases Nova-3 open weights model", "https://acme.ai/blog/nova-3/?utm_source=rss", "Same link, tracking param."),
        ("EU AI Act guidance on general purpose models published", "https://wire.example/eu-gpai", "The AI Office published guidance."),
    ])
    return WORK


@pytest.fixture(scope="session")
def db(workdir):
    import psycopg

    with psycopg.connect(TEST_DB, autocommit=True) as c:
        c.execute("drop schema public cascade")
        c.execute("create schema public")
    from app.seed import bootstrap

    bootstrap()
    yield
    from app.db import close_pool

    close_pool()


def pytest_collection_modifyitems(items):
    """The pipeline tests build the data the API tests read, so run them first."""
    order = {"test_normalize.py": 0, "test_llm.py": 0, "test_pipeline.py": 1, "test_api.py": 2, "test_sources.py": 3}
    items.sort(key=lambda i: order.get(i.fspath.basename, 9))
