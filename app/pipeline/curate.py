"""Stage 2: decide what earns a place in the feed, and describe it."""
from __future__ import annotations

import logging
import string
from pathlib import Path

from ..config import Topic, settings
from ..db import conn
from ..normalize import utcnow

log = logging.getLogger("news.curate")
PROMPTS = Path(__file__).resolve().parent / "prompts"
BATCH = 40

CONTENT_TYPES = ["official", "article", "podcast", "video", "paper", "repo", "discussion", "legislation"]
STAGES = ["proposed", "passed", "in_force", "enforcement", "guidance"]
OFFICIAL_KINDS = {"company", "lab", "regulator"}  # their own channels are primary sources

SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "keep": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                    "lens_score": {"type": "integer", "minimum": 0, "maximum": 3},
                    "summary": {"type": "string"},
                    "content_type": {"type": "string", "enum": CONTENT_TYPES},
                    "categories": {"type": "array", "items": {"type": "string"}},
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "legislation_stage": {"type": ["string", "null"], "enum": STAGES + [None]},
                    "jurisdiction": {"type": ["string", "null"]},
                },
                "required": ["id", "keep", "reason"],
            },
        }
    },
    "required": ["results"],
}


def team_feedback(limit: int = 15) -> dict:
    with conn() as c:
        flagged = c.execute(
            """select p.title from feedback f join posts p on p.id = f.post_id
               where f.kind = 'not_relevant' and f.created_at > now() - interval '45 days'
               group by p.id, p.title order by max(f.created_at) desc limit %s""",
            (limit,),
        ).fetchall()
        upvoted = c.execute(
            """select p.title from votes v join posts p on p.id = v.post_id
               where v.created_at > now() - interval '30 days'
               group by p.id, p.title order by count(*) desc, max(v.created_at) desc limit %s""",
            (limit,),
        ).fetchall()
    return {"not_relevant": [r["title"] for r in flagged], "upvoted": [r["title"] for r in upvoted]}


def _org_context(candidates: list[dict]) -> tuple[dict, list[str]]:
    from ..sources.registry import known_org_names

    ids = sorted({c["org_id"] for c in candidates if c.get("org_id")})
    with conn() as c:
        rows = c.execute("select id, name, kind from orgs where id = any(%s)", (ids,)).fetchall() if ids else []
        names = known_org_names(c)
    return {r["id"]: r for r in rows}, names


def curate(llm, topic: Topic, candidates: list[dict], stats: dict) -> list[dict]:
    """Returns kept candidates, each enriched with the curator's description."""
    if not candidates:
        return []
    system = string.Template((PROMPTS / "curate.md").read_text()).safe_substitute(
        topic_label=topic.label, topic_description=topic.description, lens=topic.lens.strip()
    )
    taxonomy = [{"slug": t["slug"], "description": t.get("description", "")} for t in topic.taxonomy]
    allowed = {t["slug"] for t in topic.taxonomy}
    feedback = team_feedback()
    orgs, known_names = _org_context(candidates)
    kept: list[dict] = []
    by_id = {c["id"]: c for c in candidates}

    for start in range(0, len(candidates), BATCH):
        batch = candidates[start : start + BATCH]
        payload = {
            "today": utcnow().date().isoformat(),
            "taxonomy": taxonomy,
            "team_feedback": feedback,
            "known_organisations": known_names,
            "items": [
                {
                    "id": c["id"],
                    "title": c["title"],
                    "url": c["url"],
                    "source": c["source_name"],
                    "tier": c["tier"],
                    "found_via": c["found_via"],
                    "published": c["published_at"].isoformat() if c["published_at"] else None,
                    "excerpt": c["excerpt"],
                    "org": orgs[c["org_id"]]["name"] if c.get("org_id") in orgs else None,
                    "official": bool(c.get("org_id") in orgs and orgs[c["org_id"]]["kind"] in OFFICIAL_KINDS),
                }
                for c in batch
            ],
        }
        result = llm.structured(
            model=settings.curate_model, system=system, payload=payload,
            tool_name="curate_results", schema=SCHEMA,
        )
        answered = set()
        with conn() as c:
            for r in result.get("results", []):
                cand = by_id.get(r.get("id"))
                if cand is None or r["id"] in answered:
                    continue
                answered.add(r["id"])
                if not r.get("keep"):
                    c.execute("update candidates set decision = 'rejected', reason = %s where id = %s",
                              ((r.get("reason") or "rejected")[:300], cand["id"]))
                    stats["rejected"] = stats.get("rejected", 0) + 1
                    continue
                cats = [s for s in (r.get("categories") or []) if s in allowed][:3]
                enriched = {
                    **cand,
                    "summary": (r.get("summary") or cand["excerpt"] or cand["title"]).strip()[:600],
                    "importance": max(1, min(5, int(r.get("importance") or 2))),
                    "lens_score": max(0, min(3, int(r.get("lens_score") or 0))),
                    "content_type": r.get("content_type") if r.get("content_type") in CONTENT_TYPES else "article",
                    "categories": cats,
                    "entities": [e.strip() for e in (r.get("entities") or []) if e and e.strip()][:3],
                    "legislation_stage": r.get("legislation_stage") if "legislation" in cats else None,
                    "jurisdiction": r.get("jurisdiction") if "legislation" in cats and r.get("jurisdiction") in allowed else None,
                }
                c.execute("update candidates set decision = 'kept', reason = %s where id = %s",
                          ((r.get("reason") or "kept")[:300], cand["id"]))
                kept.append(enriched)
                stats["kept"] = stats.get("kept", 0) + 1
            for cand in batch:
                if cand["id"] not in answered:
                    c.execute("update candidates set decision = 'skipped', reason = 'curator returned no verdict' where id = %s",
                              (cand["id"],))
    return kept
