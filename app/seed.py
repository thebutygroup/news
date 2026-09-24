"""Sync config into the database on every startup. It never deletes anything.

  registry/orgs.json        organisations and their channels (see app/sources/registry.py)
  topics/<slug>/*.json      topic tags, lens tag, taxonomy, and topic-specific feeds
"""
from __future__ import annotations

import logging

from .config import load_topics
from .db import conn, migrate
from .pipeline.cluster import ensure_tag
from .sources.classify import classify
from .sources.registry import seed_registry

log = logging.getLogger("news.seed")


def seed() -> None:
    with conn() as c:
        orgs = seed_registry(c, ensure_tag)
        for topic in load_topics():
            ensure_tag(c, topic.slug, topic.label, "topic", description=topic.description)
            if topic.lens_tag:
                lt = topic.lens_tag
                ensure_tag(c, lt["slug"], lt["label"], "lens", description=lt.get("description"))
            for t in topic.taxonomy:
                tid = ensure_tag(c, t["slug"], t.get("label", t["slug"]), "category", description=t.get("description"))
                c.execute("update tags set description = %s, label = %s where id = %s and created_by = 'system'",
                          (t.get("description"), t.get("label", t["slug"]), tid))
            for s in topic.sources:
                kind = s.get("kind")
                if kind == "hn":
                    url, channel = f"hn:{topic.slug}:{s.get('query', '')}", "community"
                else:
                    channel, kind, url = classify(s["url"], s.get("channel"))
                    kind = s.get("read", kind)
                org = c.execute("select id from orgs where slug = %s", (s["org"],)).fetchone() if s.get("org") else None
                c.execute(
                    """insert into sources (topic, name, url, homepage, kind, channel, tier, query, min_points, org_id,
                                            origin, link_pattern)
                       values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'seed', %s)
                       on conflict (url) do update set name = excluded.name, tier = excluded.tier,
                         homepage = excluded.homepage, query = excluded.query, min_points = excluded.min_points,
                         kind = excluded.kind, channel = excluded.channel, topic = excluded.topic,
                         org_id = coalesce(excluded.org_id, sources.org_id), link_pattern = excluded.link_pattern""",
                    (topic.slug, s["name"], url, s.get("homepage"), kind, s.get("channel", channel), s.get("tier", 1),
                     s.get("query"), s.get("min_points"), org["id"] if org else None, s.get("link_pattern")),
                )
    log.info("seed complete: %d orgs in registry", orgs)


def bootstrap() -> None:
    applied = migrate()
    if applied:
        log.info("applied migrations: %s", applied)
    seed()
