"""Settings from environment variables, plus topic config loaded from /topics.

Every topic is a folder under topics/ with:
  topic.json     name, description, discovery queries, lens tag, similar services
  lens.md        who the feed is for and what to boost
  sources.json   RSS/Atom feeds and Hacker News queries
  taxonomy.json  category tags the curator may apply
Adding a topic means adding a folder. No code changes.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _list(name: str) -> list[str]:
    return [v.strip().lower() for v in (_env(name, "") or "").split(",") if v.strip()]


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "postgresql://news:news@db:5432/news"))
    timezone: str = field(default_factory=lambda: _env("APP_TIMEZONE", "Europe/London"))
    scan_cron: str = field(default_factory=lambda: _env("SCAN_CRON", "0 4,13 * * *"))
    topics_dir: Path = field(default_factory=lambda: Path(_env("TOPICS_DIR", str(ROOT / "topics"))))
    static_dir: Path = field(default_factory=lambda: Path(_env("STATIC_DIR", str(ROOT / "static"))))
    registry_path: Path = field(default_factory=lambda: Path(_env("REGISTRY_PATH", str(ROOT / "registry" / "orgs.json"))))

    # LLM
    anthropic_api_key: str | None = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    llm_fake: bool = field(default_factory=lambda: _env("LLM_FAKE", "0") == "1")
    curate_model: str = field(default_factory=lambda: _env("CURATE_MODEL", "claude-sonnet-5"))
    cluster_model: str = field(default_factory=lambda: _env("CLUSTER_MODEL", "claude-sonnet-5"))
    discovery_model: str = field(default_factory=lambda: _env("DISCOVERY_MODEL", "claude-sonnet-5"))
    web_search_tool: str = field(default_factory=lambda: _env("WEB_SEARCH_TOOL", "web_search_20250305"))
    max_searches_per_run: int = field(default_factory=lambda: _int("MAX_SEARCHES_PER_RUN", 12))
    max_llm_calls_per_run: int = field(default_factory=lambda: _int("MAX_LLM_CALLS_PER_RUN", 40))
    max_candidates_per_run: int = field(default_factory=lambda: _int("MAX_CANDIDATES_PER_RUN", 250))

    # Collection
    lookback_hours: int = field(default_factory=lambda: _int("LOOKBACK_HOURS", 36))
    first_run_lookback_hours: int = field(default_factory=lambda: _int("FIRST_RUN_LOOKBACK_HOURS", 72))
    max_items_per_feed: int = field(default_factory=lambda: _int("MAX_ITEMS_PER_FEED", 30))
    source_fail_pause_after: int = field(default_factory=lambda: _int("SOURCE_FAIL_PAUSE_AFTER", 5))
    proposed_source_threshold: int = field(default_factory=lambda: _int("PROPOSED_SOURCE_THRESHOLD", 3))
    fetch_workers: int = field(default_factory=lambda: _int("FETCH_WORKERS", 8))
    page_metadata_per_source: int = field(default_factory=lambda: _int("PAGE_METADATA_PER_SOURCE", 8))
    watch_check_hours: int = field(default_factory=lambda: _int("WATCH_CHECK_HOURS", 24))

    # Known sources: organisations
    org_propose_threshold: int = field(default_factory=lambda: _int("ORG_PROPOSE_THRESHOLD", 3))
    org_propose_window_days: int = field(default_factory=lambda: _int("ORG_PROPOSE_WINDOW_DAYS", 14))
    org_sweep_per_run: int = field(default_factory=lambda: _int("ORG_SWEEP_PER_RUN", 8))
    channel_discovery_per_run: int = field(default_factory=lambda: _int("CHANNEL_DISCOVERY_PER_RUN", 5))

    # Daily audio briefing
    podcast_enabled: bool = field(default_factory=lambda: _env("PODCAST_ENABLED", "1") == "1")
    podcast_cron: str = field(default_factory=lambda: _env("PODCAST_CRON", "30 5 * * *"))
    podcast_model: str = field(default_factory=lambda: _env("PODCAST_MODEL", "claude-haiku-4-5-20251001"))
    podcast_tts: str = field(default_factory=lambda: _env("PODCAST_TTS", "edge"))  # edge | azure | fake
    podcast_host_a: str = field(default_factory=lambda: _env("PODCAST_HOST_A", "Alex"))
    podcast_host_b: str = field(default_factory=lambda: _env("PODCAST_HOST_B", "Sam"))
    podcast_voice_a: str = field(default_factory=lambda: _env("PODCAST_VOICE_A", "en-GB-RyanNeural"))
    podcast_voice_b: str = field(default_factory=lambda: _env("PODCAST_VOICE_B", "en-GB-SoniaNeural"))
    podcast_stories: int = field(default_factory=lambda: _int("PODCAST_STORIES", 8))
    podcast_words: int = field(default_factory=lambda: _int("PODCAST_WORDS", 800))
    podcast_keep: int = field(default_factory=lambda: _int("PODCAST_KEEP", 30))
    azure_speech_key: str | None = field(default_factory=lambda: _env("AZURE_SPEECH_KEY"))
    azure_speech_region: str = field(default_factory=lambda: _env("AZURE_SPEECH_REGION", "uksouth"))
    media_dir: Path = field(default_factory=lambda: Path(_env("MEDIA_DIR", "/data/media")))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "https://news.thebutygroup.com").rstrip("/"))

    # X (optional, pay per use)
    x_bearer_token: str | None = field(default_factory=lambda: _env("X_BEARER_TOKEN"))
    x_max_reads_per_run: int = field(default_factory=lambda: _int("X_MAX_READS_PER_RUN", 300))

    # Stories
    story_tag_threshold: int = field(default_factory=lambda: _int("STORY_TAG_THRESHOLD", 2))
    story_window_days: int = field(default_factory=lambda: _int("STORY_WINDOW_DAYS", 21))

    # Identity (Cloudflare Access)
    cf_access_team_domain: str | None = field(default_factory=lambda: _env("CF_ACCESS_TEAM_DOMAIN"))
    cf_access_aud: str | None = field(default_factory=lambda: _env("CF_ACCESS_AUD"))
    trust_cf_email_header: bool = field(default_factory=lambda: _env("TRUST_CF_EMAIL_HEADER", "0") == "1")
    dev_user_email: str | None = field(default_factory=lambda: _env("DEV_USER_EMAIL"))
    admin_emails: list[str] = field(default_factory=lambda: _list("ADMIN_EMAILS"))
    # Extra hosts allowed to send writes, e.g. news.thebutygroup.com if a proxy rewrites Host.
    allowed_hosts: list[str] = field(default_factory=lambda: _list("ALLOWED_HOSTS"))


settings = Settings()


@dataclass
class Topic:
    slug: str
    label: str
    description: str
    lens: str
    lens_tag: dict | None
    discovery_queries: list[str]
    sources: list[dict]
    taxonomy: list[dict]
    similar: list[dict]


def load_topics(topics_dir: Path | None = None) -> list[Topic]:
    base = topics_dir or settings.topics_dir
    topics: list[Topic] = []
    for folder in sorted(p for p in base.iterdir() if p.is_dir() and (p / "topic.json").exists()):
        meta = json.loads((folder / "topic.json").read_text())
        if meta.get("enabled", True) is False:
            continue
        topics.append(
            Topic(
                slug=meta["slug"],
                label=meta["label"],
                description=meta.get("description", ""),
                lens=(folder / "lens.md").read_text() if (folder / "lens.md").exists() else "",
                lens_tag=meta.get("lens_tag"),
                discovery_queries=meta.get("discovery_queries", []),
                sources=json.loads((folder / "sources.json").read_text()) if (folder / "sources.json").exists() else [],
                taxonomy=json.loads((folder / "taxonomy.json").read_text()) if (folder / "taxonomy.json").exists() else [],
                similar=meta.get("similar", []),
            )
        )
    return topics
