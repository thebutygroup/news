"""Stage 3: group kept items into stories and decide what reaches the feed.

For each kept item:
  new story                      -> becomes a post
  existing story, adds new facts -> becomes a post with a one-line "what's new" delta
  existing story, nothing new    -> folded into the story's latest post as extra coverage
When a story reaches STORY_TAG_THRESHOLD articles it gets its own tag, so readers can
follow it or hide it.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from ..config import Topic, settings
from ..db import conn
from ..normalize import slugify, utcnow

log = logging.getLogger("news.cluster")
PROMPTS = Path(__file__).resolve().parent / "prompts"
BATCH = 25
TYPE_RANK = {"official": 0, "legislation": 0, "paper": 1, "repo": 1}

SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "story": {"type": "string"},
                    "new_information": {"type": "boolean"},
                    "delta": {"type": ["string", "null"]},
                },
                "required": ["id", "story", "new_information"],
            },
        },
        "new_stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "headline": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "tag_slug": {"type": "string"},
                },
                "required": ["ref", "headline"],
            },
        },
    },
    "required": ["assignments", "new_stories"],
}


# -- tags ------------------------------------------------------------------
def ensure_tag(c, slug: str, label: str, type_: str, created_by: str = "system", description: str | None = None) -> int:
    row = c.execute(
        """insert into tags (slug, label, type, created_by, description) values (%s, %s, %s, %s, %s)
           on conflict (slug) do nothing returning id""",
        (slug, label, type_, created_by, description),
    ).fetchone()
    if row:
        return row["id"]
    return c.execute("select id from tags where slug = %s", (slug,)).fetchone()["id"]


def attach_tag(c, post_id: int, tag_id: int, added_by: str = "curator") -> None:
    c.execute(
        "insert into post_tags (post_id, tag_id, added_by) values (%s, %s, %s) on conflict do nothing",
        (post_id, tag_id, added_by),
    )


def tag_post(c, post_id: int, topic: Topic, item: dict, story_tag_id: int | None, resolver=None) -> None:
    attach_tag(c, post_id, ensure_tag(c, topic.slug, topic.label, "topic"))
    labels = {t["slug"]: t.get("label", t["slug"]) for t in topic.taxonomy}
    for slug in item.get("categories", []):
        attach_tag(c, post_id, ensure_tag(c, slug, labels.get(slug, slug), "category"))
    if item.get("jurisdiction"):
        j = item["jurisdiction"]
        attach_tag(c, post_id, ensure_tag(c, j, labels.get(j, j), "category"))
    for name in item.get("entities", []):
        org_tag = resolver.tag_for(name) if resolver else None
        slug = slugify(name, 40)
        if org_tag:
            attach_tag(c, post_id, org_tag)
        elif slug:
            attach_tag(c, post_id, ensure_tag(c, slug, name, "entity"))
    if item.get("org_id"):
        row = c.execute("select tag_id from orgs where id = %s", (item["org_id"],)).fetchone()
        if row and row["tag_id"]:
            attach_tag(c, post_id, row["tag_id"])
    lens = topic.lens_tag
    if lens and item.get("lens_score", 0) >= int(lens.get("threshold", 2)):
        attach_tag(c, post_id, ensure_tag(c, lens["slug"], lens["label"], "lens", description=lens.get("description")))
    if story_tag_id:
        attach_tag(c, post_id, story_tag_id)


# -- stories ---------------------------------------------------------------
def active_stories(items: list[dict]) -> list[dict]:
    since = utcnow() - timedelta(days=settings.story_window_days)
    with conn() as c:
        ids = [r["id"] for r in c.execute(
            "select id from stories where last_update_at > %s order by last_update_at desc limit 60", (since,)
        ).fetchall()]
        for item in items:
            for r in c.execute(
                """select id from stories where last_update_at > %s and similarity(headline, %s) > 0.15
                   order by similarity(headline, %s) desc limit 4""",
                (since, item["title"], item["title"]),
            ).fetchall():
                if r["id"] not in ids:
                    ids.append(r["id"])
        ids = ids[:120]
        if not ids:
            return []
        rows = c.execute(
            """select s.id, s.headline, s.keywords, s.last_update_at,
                      coalesce((select json_agg(x) from (
                          select coalesce(p.delta, p.title) as update from posts p
                          where p.story_id = s.id order by p.feed_at desc limit 3) x), '[]') as latest
               from stories s where s.id = any(%s) order by s.last_update_at desc""",
            (ids,),
        ).fetchall()
    return [
        {"id": r["id"], "headline": r["headline"], "keywords": r["keywords"],
         "last_update": r["last_update_at"].date().isoformat(), "latest": [u["update"] for u in r["latest"]]}
        for r in rows
    ]


def _feed_at(published):
    now = utcnow()
    if published and published <= now and now - published <= timedelta(days=3):
        return published
    return now


def _insert_post(c, topic: Topic, item: dict, story_id: int, delta: str | None, story_tag_id: int | None) -> int | None:
    row = c.execute(
        """insert into posts (story_id, url, canonical_url, title, summary, delta, source_name, content_type,
                              published_at, feed_at, importance, lens_score, legislation_stage, jurisdiction)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           on conflict (canonical_url) do nothing returning id""",
        (story_id, item["url"], item["canonical_url"], item["title"], item["summary"], delta, item["source_name"],
         item["content_type"], item["published_at"], _feed_at(item["published_at"]), item["importance"],
         item["lens_score"], item["legislation_stage"], item["jurisdiction"]),
    ).fetchone()
    if not row:
        # Same URL already posted, most likely under another topic. Tag it with this topic too.
        existing = c.execute("select id from posts where canonical_url = %s", (item["canonical_url"],)).fetchone()
        if existing:
            attach_tag(c, existing["id"], ensure_tag(c, topic.slug, topic.label, "topic"))
        return None
    from ..sources.registry import EntityResolver

    tag_post(c, row["id"], topic, item, story_tag_id, EntityResolver(c))
    return row["id"]


def _insert_coverage(c, story_id: int, item: dict) -> bool:
    lead = c.execute(
        "select id from posts where story_id = %s order by feed_at desc limit 1", (story_id,)
    ).fetchone()
    if not lead:
        return False
    row = c.execute(
        """insert into coverage (story_id, post_id, url, canonical_url, title, source_name, published_at)
           values (%s, %s, %s, %s, %s, %s, %s) on conflict (canonical_url) do nothing returning id""",
        (story_id, lead["id"], item["url"], item["canonical_url"], item["title"], item["source_name"], item["published_at"]),
    ).fetchone()
    return row is not None


def cluster_and_post(llm, topic: Topic, kept: list[dict], stats: dict) -> None:
    if not kept:
        return
    system = (PROMPTS / "cluster.md").read_text()
    kept = sorted(kept, key=lambda i: (i["published_at"] or utcnow()))
    for start in range(0, len(kept), BATCH):
        batch = kept[start : start + BATCH]
        stories = active_stories(batch)
        result = llm.structured(
            model=settings.cluster_model,
            system=system,
            payload={
                "today": utcnow().date().isoformat(),
                "active_stories": stories,
                "items": [
                    {"id": i["id"], "title": i["title"], "source": i["source_name"], "summary": i["summary"],
                     "published": i["published_at"].isoformat() if i["published_at"] else None,
                     "categories": i["categories"], "content_type": i["content_type"]}
                    for i in batch
                ],
            },
            tool_name="cluster_results",
            schema=SCHEMA,
        )
        _apply(topic, batch, stories, result, stats)


def _apply(topic: Topic, batch: list[dict], stories: list[dict], result: dict, stats: dict) -> None:
    by_id = {i["id"]: i for i in batch}
    valid_story_ids = {str(s["id"]) for s in stories}
    new_meta = {s["ref"]: s for s in result.get("new_stories", []) if s.get("ref")}

    groups: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    seen = set()
    for a in result.get("assignments", []):
        item = by_id.get(a.get("id"))
        if item is None or item["id"] in seen:
            continue
        seen.add(item["id"])
        ref = str(a.get("story") or "")
        if ref not in valid_story_ids and ref not in new_meta:
            ref = f"new:auto:{item['id']}"  # model pointed at a story it was not shown; treat as new
        groups[ref].append((item, a))
    for item in batch:  # anything the model skipped becomes its own story rather than vanishing
        if item["id"] not in seen:
            groups[f"new:auto:{item['id']}"].append((item, {"new_information": True, "delta": None}))

    with conn() as c:
        for ref, members in groups.items():
            if ref in valid_story_ids:
                story_id = int(ref)
                story_tag_id = c.execute("select story_tag_id from stories where id = %s", (story_id,)).fetchone()["story_tag_id"]
                for item, a in members:
                    if a.get("new_information") and (a.get("delta") or "").strip():
                        post_id = _insert_post(c, topic, item, story_id, a["delta"].strip()[:300], story_tag_id)
                        _mark(c, item, "post" if post_id else "coverage", post_id, "follow-up with new information")
                        stats["follow_up_posts" if post_id else "coverage"] = stats.get("follow_up_posts" if post_id else "coverage", 0) + 1
                    else:
                        added = _insert_coverage(c, story_id, item)
                        _mark(c, item, "coverage", None, "same story, nothing new")
                        stats["coverage"] = stats.get("coverage", 0) + (1 if added else 0)
                    c.execute("update stories set article_count = article_count + 1, last_update_at = now() where id = %s",
                              (story_id,))
                continue

            # New story: lead with the primary source, fold the rest in as coverage.
            members.sort(key=lambda m: (TYPE_RANK.get(m[0]["content_type"], 2), -m[0]["importance"],
                                        m[0]["published_at"] or utcnow()))
            meta = new_meta.get(ref, {})
            lead = members[0][0]
            headline = (meta.get("headline") or lead["title"]).strip()[:200]
            month = utcnow().strftime("%b-%Y").lower()
            proposed = slugify(meta.get("tag_slug") or f"{' '.join(headline.split()[:4])} {month}", 50)
            story_id = c.execute(
                """insert into stories (headline, keywords, proposed_tag, article_count) values (%s, %s, %s, %s)
                   returning id""",
                (headline, [k.lower() for k in (meta.get("keywords") or [])][:8], proposed, len(members)),
            ).fetchone()["id"]
            stats["stories_new"] = stats.get("stories_new", 0) + 1
            post_id = _insert_post(c, topic, lead, story_id, None, None)
            _mark(c, lead, "post" if post_id else "coverage", post_id, "new story")
            stats["posts"] = stats.get("posts", 0) + (1 if post_id else 0)
            for item, _ in members[1:]:
                _insert_coverage(c, story_id, item)
                _mark(c, item, "coverage", None, "same new story as another item in this scan")
                stats["coverage"] = stats.get("coverage", 0) + 1


def _mark(c, item: dict, decision: str, post_id: int | None, reason: str) -> None:
    c.execute("update candidates set decision = %s, post_id = %s, reason = %s where id = %s",
              (decision, post_id, reason, item["id"]))


def materialize_story_tags(stats: dict) -> None:
    """Big stories get their own tag so they can be followed or hidden."""
    with conn() as c:
        rows = c.execute(
            """select id, headline, proposed_tag from stories
               where story_tag_id is null and article_count >= %s and last_update_at > now() - interval '7 days'""",
            (settings.story_tag_threshold,),
        ).fetchall()
        for s in rows:
            slug = s["proposed_tag"] or slugify(s["headline"], 50)
            if c.execute("select 1 from tags where slug = %s and type <> 'story'", (slug,)).fetchone():
                slug = slugify(f"{slug}-story", 50)
            tag_id = ensure_tag(c, slug, slug.replace("-", " "), "story", description=s["headline"])
            c.execute("update stories set story_tag_id = %s where id = %s", (tag_id, s["id"]))
            for p in c.execute("select id from posts where story_id = %s", (s["id"],)).fetchall():
                attach_tag(c, p["id"], tag_id)
            stats["story_tags_created"] = stats.get("story_tags_created", 0) + 1
