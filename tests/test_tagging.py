"""Tagging: an item gets its category, organisation, place and subject tags the first time."""
from app.places import place_tag


def test_place_names_collapse():
    assert place_tag("Britain") == ("uk", "UK")
    assert place_tag("the US") == ("us", "US")
    assert place_tag("Australian") == ("australia", "Australia")
    assert place_tag("Tallinn") == ("tallinn", "Tallinn")


def test_dry_run_curator_finds_orgs_and_places():
    from app.pipeline.fake_llm import FakeLLM

    out = FakeLLM()._curate({
        "taxonomy": [{"slug": "security"}],
        "known_organisations": ["OpenAI", "Anthropic"],
        "items": [{"id": 1, "url": "https://x.example/a", "title": "OpenAI confirms hackers stole Australian customer data",
                   "excerpt": "The breach hit accounts in Australia."}],
    })["results"][0]
    assert out["entities"] == ["OpenAI"]
    assert out["places"] == ["australia"]
    assert "security" in out["categories"] and out["extra_tags"] == ["data-breach"]


def test_tag_post_applies_every_kind(db):
    from app.config import load_topics
    from app.db import conn
    from app.pipeline.cluster import tag_post
    from app.sources.registry import EntityResolver

    topic = load_topics()[0]
    item = {"categories": ["security"], "entities": ["Acme"], "places": ["Australian"], "extra_tags": ["Data breach"],
            "lens_score": 0}
    with conn() as c:
        pid = c.execute("""insert into posts (url, canonical_url, title, summary) values
                           ('https://t.example/x', 'https://t.example/x', 'Acme hack', 's') returning id""").fetchone()["id"]
        tag_post(c, pid, topic, item, None, EntityResolver(c))
        tags = {r["slug"]: r["type"] for r in c.execute(
            "select t.slug, t.type from post_tags pt join tags t on t.id = pt.tag_id where pt.post_id = %s", (pid,)).fetchall()}
        c.execute("delete from posts where id = %s", (pid,))
    # "Acme" is an alias of the Acme AI org in the test registry, so it resolves to the org's tag.
    assert tags == {"ai": "topic", "security": "category", "acme-ai": "entity", "australia": "place",
                    "data-breach": "subject"}


def test_typeahead_matches_aliases(db):
    from app.queries import suggest_tags

    got = suggest_tags("hack")
    assert got[0]["slug"] == "security" and got[0]["matched"] == "hack"
