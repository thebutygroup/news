"""Feed filtering.

URL grammar, flat and hand-editable:
  ?t=ai            include posts tagged ai
  ?t=-legislation  exclude posts tagged legislation
  ?q=agents        full-text search (supports "quoted phrases" and -minus words)
  ?sort=top        within each day, most important first (default: newest first)
  ?before=<iso>    pagination cursor
Multiple t params AND together. Unknown tags are ignored and reported back.
"""
from __future__ import annotations

from datetime import datetime

from .config import settings
from .db import conn

POST_COLUMNS = """
  p.id, p.story_id, p.url, p.title, p.summary, p.delta, p.source_name, p.content_type,
  p.published_at, p.feed_at, p.importance, p.lens_score, p.legislation_stage, p.jurisdiction, p.posted_by,
  s.headline as story_headline, s.article_count as story_article_count,
  (select count(*) from posts p2 where p2.story_id = p.story_id) as story_post_count,
  (select count(*) from votes v where v.post_id = p.id) as votes,
  exists(select 1 from votes v where v.post_id = p.id and v.user_email = %(me)s) as voted,
  exists(select 1 from feedback f where f.post_id = p.id and f.user_email = %(me)s and f.kind = 'not_relevant') as flagged,
  (select count(*) from comments cm where cm.post_id = p.id) as comment_count,
  (select count(*) from coverage cv where cv.post_id = p.id) as coverage_count,
  coalesce((
    select json_agg(json_build_object('slug', t.slug, 'label', t.label, 'type', t.type, 'added_by', pt.added_by)
                    order by case t.type when 'topic' then 0 when 'story' then 1 when 'lens' then 2
                                         when 'category' then 3 when 'entity' then 4 else 5 end, t.slug)
    from post_tags pt join tags t on t.id = pt.tag_id where pt.post_id = p.id
  ), '[]') as tags
"""


def parse_tag_params(values: list[str]) -> tuple[list[str], list[str]]:
    include, exclude = [], []
    for raw in values:
        for v in raw.split(","):
            v = v.strip().lower()
            if not v or v in ("-", "+"):
                continue
            (exclude if v.startswith("-") else include).append(v.lstrip("-+"))
    return sorted(set(include)), sorted(set(exclude) - set(include))


def known_tags(slugs: list[str]) -> set[str]:
    if not slugs:
        return set()
    with conn() as c:
        return {r["slug"] for r in c.execute("select slug from tags where slug = any(%s)", (slugs,)).fetchall()}


def feed(me: str | None, tag_values: list[str], q: str | None, sort: str | None,
         before: datetime | None, limit: int = 120) -> dict:
    include, exclude = parse_tag_params(tag_values)
    known = known_tags(include + exclude)
    unknown = [t for t in include + exclude if t not in known]
    include = [t for t in include if t in known]
    exclude = [t for t in exclude if t in known]

    where = ["not p.hidden"]
    params: dict = {"me": me or "", "limit": min(max(limit, 1), 300), "tz": settings.timezone}
    if include:
        where.append(
            """p.id in (select pt.post_id from post_tags pt join tags t on t.id = pt.tag_id
                        where t.slug = any(%(inc)s) group by pt.post_id having count(distinct t.slug) = %(n_inc)s)"""
        )
        params["inc"], params["n_inc"] = include, len(include)
    if exclude:
        where.append(
            """not exists (select 1 from post_tags pt join tags t on t.id = pt.tag_id
                           where pt.post_id = p.id and t.slug = any(%(exc)s))"""
        )
        params["exc"] = exclude
    query = (q or "").strip()
    if query:
        where.append("p.search @@ websearch_to_tsquery('english', %(q)s)")
        params["q"] = query
    if before:
        where.append("p.feed_at < %(before)s")
        params["before"] = before

    if sort == "top":
        order = """(p.feed_at at time zone %(tz)s)::date desc,
                   (p.importance * 2 + (select count(*) from votes v where v.post_id = p.id)) desc, p.feed_at desc"""
    else:
        order = "p.feed_at desc"

    sql = f"""select {POST_COLUMNS}, to_char(p.feed_at at time zone %(tz)s, 'YYYY-MM-DD') as day
              from posts p left join stories s on s.id = p.story_id
              where {' and '.join(where)}
              order by {order}
              limit %(limit)s"""
    with conn() as c:
        rows = c.execute(sql, params).fetchall()
    next_before = None
    if len(rows) == params["limit"]:
        next_before = min(r["feed_at"] for r in rows).isoformat()
    return {
        "posts": rows,
        "next_before": next_before,
        "filters": {"include": include, "exclude": exclude, "q": query, "sort": sort or "new"},
        "unknown_tags": unknown,
    }


def suggest_tags(prefix: str, limit: int = 12) -> list[dict]:
    """Tags whose slug, label or aliases start with (or look like) what was typed.
    'matched' says which alias matched, so the UI can show "#security, matches hack"."""
    prefix = (prefix or "").strip().lower().lstrip("-+#")
    with conn() as c:
        if not prefix:
            return c.execute(
                """select t.slug, t.label, t.type, count(pt.post_id) as uses, null as matched from tags t
                   left join post_tags pt on pt.tag_id = t.id group by t.id
                   order by uses desc, t.slug limit %s""",
                (limit,),
            ).fetchall()
        return c.execute(
            """select t.slug, t.label, t.type, count(pt.post_id) as uses,
                      (select a from unnest(t.aliases) a where a like %(p)s || '%%' order by length(a) limit 1) as matched
               from tags t left join post_tags pt on pt.tag_id = t.id
               where t.slug like %(p)s || '%%' or t.label ilike '%%' || %(p)s || '%%'
                  or exists (select 1 from unnest(t.aliases) a where a like %(p)s || '%%')
                  or similarity(t.slug, %(p)s) > 0.35
               group by t.id
               order by (t.slug like %(p)s || '%%') desc, count(pt.post_id) desc, t.slug
               limit %(limit)s""",
            {"p": prefix, "limit": limit},
        ).fetchall()
