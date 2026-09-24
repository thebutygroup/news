"""Which organisations matter, and which ones should we start following?

Two signals, both automatic:
  mentions     the curator names the organisations central to each post; those become entity
               tags. An org's mention_count is its tagged posts in the last 30 days.
  search hits  web search keeps landing on a site that isn't in the registry.
An entity with ORG_PROPOSE_THRESHOLD posts in ORG_PROPOSE_WINDOW_DAYS, or a site with that many
search hits, becomes a *proposed* org. A person follows or dismisses it on the Sources page.
Following it triggers channel discovery on its homepage.

The sweep: followed orgs with no working polled channel (only X or LinkedIn, say) are handed to
web search a few at a time, oldest-checked first, so they still get looked at.
"""
from __future__ import annotations

import logging
from collections import Counter

from ..config import settings
from ..db import conn
from ..normalize import domain, slugify, utcnow

log = logging.getLogger("news.relevance")


def update_mentions() -> None:
    with conn() as c:
        c.execute("update orgs set mention_count = 0")
        c.execute(
            """update orgs o set mention_count = s.n, last_mentioned_at = s.last
               from (select o2.id, count(distinct p.id) as n, max(p.feed_at) as last
                     from orgs o2 join post_tags pt on pt.tag_id = o2.tag_id join posts p on p.id = pt.post_id
                     where p.feed_at > now() - interval '30 days' and not p.hidden group by o2.id) s
               where o.id = s.id"""
        )


def _guess_homepage(c, tag_id: int, slug: str) -> str | None:
    """If the org's own site shows up among its posts' links, that's probably its homepage."""
    rows = c.execute(
        """select p.url from post_tags pt join posts p on p.id = pt.post_id where pt.tag_id = %s
           union all select cv.url from coverage cv join post_tags pt on pt.post_id = cv.post_id where pt.tag_id = %s""",
        (tag_id, tag_id),
    ).fetchall()
    compact = slug.replace("-", "")
    hosts = Counter(domain(r["url"]) for r in rows)
    for host, _ in hosts.most_common():
        label = host.split(".")[0].replace("-", "")
        if label and (label == compact or (len(label) >= 4 and (label in compact or compact in label))):
            return f"https://{host}"
    return None


def propose_from_mentions() -> list[str]:
    proposed = []
    with conn() as c:
        rows = c.execute(
            """select t.id, t.slug, t.label, count(distinct p.id) as n
               from tags t join post_tags pt on pt.tag_id = t.id join posts p on p.id = pt.post_id
               where t.type = 'entity' and p.feed_at > now() - make_interval(days => %s)
                 and not exists (select 1 from orgs o where o.tag_id = t.id or o.slug = t.slug or t.slug = any(o.aliases))
               group by t.id having count(distinct p.id) >= %s""",
            (settings.org_propose_window_days, settings.org_propose_threshold),
        ).fetchall()
        for r in rows:
            c.execute(
                """insert into orgs (slug, name, kind, homepage, status, tag_id, mention_count, created_by, topics)
                   values (%s, %s, 'company', %s, 'proposed', %s, %s, 'relevance', '{ai}')
                   on conflict (slug) do nothing""",
                (r["slug"], r["label"], _guess_homepage(c, r["id"], r["slug"]), r["id"], r["n"]),
            )
            proposed.append(r["label"])
    return proposed


def note_search_hits(topic_slug: str, items: list[dict]) -> None:
    """Called with discovery results. Sites we don't know yet become proposed publications."""
    with conn() as c:
        known = {}
        for r in c.execute("select id, homepage from orgs where homepage is not null").fetchall():
            known.setdefault(domain(r["homepage"]), r["id"])
        for r in c.execute("select org_id, url from sources where org_id is not null").fetchall():
            known.setdefault(domain(r["url"]), r["org_id"])
        seen_this_run = set()
        for item in items:
            host = domain(item["url"])
            if not host or host in seen_this_run:
                continue
            seen_this_run.add(host)
            if host in known:
                c.execute("update orgs set search_hits = search_hits + 1 where id = %s", (known[host],))
                continue
            c.execute(
                """insert into orgs (slug, name, kind, homepage, status, search_hits, created_by, topics)
                   values (%s, %s, 'publication', %s, 'proposed', 1, 'search', %s)
                   on conflict (slug) do update set search_hits = orgs.search_hits + 1""",
                (slugify(host), item.get("source_name") or host, f"https://{host}", [topic_slug]),
            )


def sweep_list(topic_slug: str) -> list[dict]:
    """Followed orgs that nothing polls successfully, least recently swept first."""
    limit = settings.org_sweep_per_run
    if limit <= 0:
        return []
    with conn() as c:
        rows = c.execute(
            """select o.id, o.name, o.homepage from orgs o
               where o.status = 'following' and %s = any(o.topics)
                 and not exists (select 1 from sources s where s.org_id = o.id and s.status = 'active'
                                 and s.kind in ('rss', 'page', 'x') and s.fail_count = 0
                                 and (s.kind <> 'x' or %s))
               order by o.priority, o.last_swept_at nulls first, o.mention_count desc limit %s""",
            (topic_slug, bool(settings.x_bearer_token), limit),
        ).fetchall()
        if rows:
            c.execute("update orgs set last_swept_at = %s where id = any(%s)", (utcnow(), [r["id"] for r in rows]))
    return rows
