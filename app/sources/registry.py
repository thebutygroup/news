"""The known-sources registry: registry/orgs.json seeds the orgs and sources tables.

Ownership: the file owns an org's name, kind, homepage, topics, aliases and seeded channels.
The database owns status (following, proposed, dismissed) and priority once a row exists,
because those are changed on the Sources page. Seeding never deletes anything.
"""
from __future__ import annotations

import json
import logging

from ..config import settings
from ..normalize import slugify
from .classify import classify

log = logging.getLogger("news.registry")


def load_registry() -> list[dict]:
    path = settings.registry_path
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("orgs", [])


def seed_registry(c, ensure_tag) -> int:
    count = 0
    for o in load_registry():
        slug = o.get("slug") or slugify(o["name"])
        tag_id = ensure_tag(c, slug, o["name"], "entity")
        c.execute("update tags set aliases = %s where id = %s", ([a.lower() for a in o.get("aliases", [])], tag_id))
        aliases = [slugify(a) for a in o.get("aliases", []) if slugify(a) and slugify(a) != slug]
        priority = o.get("priority", "core")
        org_id = c.execute(
            """insert into orgs (slug, name, kind, homepage, priority, status, topics, aliases, default_tier, notes, tag_id)
               values (%s, %s, %s, %s, %s, 'following', %s, %s, %s, %s, %s)
               on conflict (slug) do update set name = excluded.name, kind = excluded.kind,
                 homepage = coalesce(excluded.homepage, orgs.homepage), topics = excluded.topics,
                 aliases = excluded.aliases, default_tier = excluded.default_tier, notes = excluded.notes,
                 tag_id = coalesce(orgs.tag_id, excluded.tag_id),
                 status = case when orgs.status = 'proposed' then 'following' else orgs.status end
               returning id""",
            (slug, o["name"], o.get("kind", "company"), o.get("homepage"), priority, o.get("topics", ["ai"]),
             aliases, o.get("tier", 2), o.get("notes"), tag_id),
        ).fetchone()["id"]
        check_hours = settings.watch_check_hours if priority == "watch" else 0
        for ch in o.get("channels", []):
            channel, kind, url = classify(ch["url"], ch.get("channel"))
            kind = ch.get("read", kind)
            c.execute(
                """insert into sources (topic, name, url, homepage, kind, channel, tier, org_id, origin, link_pattern,
                                        check_every_hours)
                   values (null, %s, %s, %s, %s, %s, %s, %s, 'seed', %s, %s)
                   on conflict (url) do update set org_id = excluded.org_id, channel = excluded.channel,
                     tier = excluded.tier, link_pattern = excluded.link_pattern, kind = excluded.kind""",
                (f"{o['name']} {channel}", url, o.get("homepage"), kind, ch.get("channel", channel),
                 ch.get("tier", o.get("tier", 2)), org_id, ch.get("link_pattern"), check_hours),
            )
        # Posts tagged under an alias before the alias existed also get the org's tag.
        if aliases:
            c.execute(
                """insert into post_tags (post_id, tag_id, added_by)
                   select pt.post_id, %s, 'registry' from post_tags pt join tags t on t.id = pt.tag_id
                   where t.slug = any(%s) on conflict do nothing""",
                (tag_id, aliases),
            )
        count += 1
    return count


class EntityResolver:
    """Maps the names the curator writes ("Google", "Alphabet") to one org tag."""

    def __init__(self, c):
        self.by_slug: dict[str, int] = {}
        for r in c.execute("select slug, aliases, tag_id from orgs where tag_id is not null and status <> 'dismissed'").fetchall():
            self.by_slug[r["slug"]] = r["tag_id"]
            for a in r["aliases"] or []:
                self.by_slug.setdefault(a, r["tag_id"])

    def tag_for(self, name: str) -> int | None:
        return self.by_slug.get(slugify(name, 40))


def known_org_names(c, limit: int = 200) -> list[str]:
    rows = c.execute(
        "select name from orgs where status = 'following' order by mention_count desc, name limit %s", (limit,)
    ).fetchall()
    return [r["name"] for r in rows]
