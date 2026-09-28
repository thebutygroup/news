from app.db import conn
from app.pipeline.run import run_scan
from tests.conftest import WORK, rss


def one(sql, *args):
    with conn() as c:
        return c.execute(sql, args).fetchone()


def test_first_scan(db):
    result = run_scan("test")
    assert result["status"] == "ok", result
    stats = result["stats"]["ai"]
    assert stats["found"] == 6
    assert stats["new"] == 5, "the utm_source copy of the Nova-3 link should collapse into one candidate"
    assert stats["rejected"] == 1
    assert one("select count(*) as n from posts")["n"] == 3
    assert one("select count(*) as n from coverage")["n"] == 1
    lead = one("select id, story_id from posts where title like 'Hugging Face%%'")
    assert one("select post_id from coverage")["post_id"] == lead["id"]
    assert one("select reason from candidates where title = 'Quarterly office party photos'")["reason"]
    # Posts from Acme's own feed carry Acme's org tag, so ?t=acme-ai works.
    nova = one("""select array_agg(t.slug) as tags from posts p join post_tags pt on pt.post_id = p.id
                  join tags t on t.id = pt.tag_id where p.title like 'Acme AI releases%%'""")
    assert "acme-ai" in nova["tags"]


def test_follow_up_scan_adds_delta_and_story_tag(db):
    rss(WORK / "wire.xml", "Tech Wire", [
        ("Hugging Face breach: attackers accessed Spaces secrets, root cause published",
         "https://wire.example/hf-root-cause", "Root cause is out."),
    ])
    result = run_scan("test")
    assert result["status"] == "ok", result
    follow = one("select delta, story_id from posts where url = 'https://wire.example/hf-root-cause'")
    assert follow and follow["delta"]
    story = one("select article_count, story_tag_id from stories where id = %s", follow["story_id"])
    assert story["article_count"] == 3
    assert story["story_tag_id"], "story should get its own tag at the threshold"
    tagged = one(
        "select count(*) as n from post_tags where tag_id = %s", story["story_tag_id"]
    )["n"]
    assert tagged == 2


def test_rescan_is_idempotent(db):
    before = one("select count(*) as n from posts")["n"]
    result = run_scan("test")
    assert result["stats"]["ai"]["new"] == 0
    assert one("select count(*) as n from posts")["n"] == before


def test_discovery_drops_unseen_urls_and_proposes_sources(db, monkeypatch):
    from app.config import load_topics
    from app.pipeline import collect
    from app.pipeline.fake_llm import FakeLLM

    def fake_search(self, **kwargs):
        return {"items": [
            {"url": "https://newsletter.example/p/big-news", "title": "Big AI news", "source_name": "New Letter"},
            {"url": "https://invented.example/nope", "title": "Made up", "source_name": "Nope"},
        ]}, {"https://newsletter.example/p/big-news"}

    monkeypatch.setattr(FakeLLM, "search_and_report", fake_search)
    topic = load_topics()[0]
    topic.discovery_queries = ["anything"]
    stats = {}
    items = collect.discover(FakeLLM(), topic, 36, stats)
    assert [i["url"] for i in items] == ["https://newsletter.example/p/big-news"]
    assert stats["discovery_unverified_dropped"] == 1
    org = one("select status, search_hits, kind from orgs where homepage = 'https://newsletter.example'")
    assert org["status"] == "proposed" and org["search_hits"] == 1 and org["kind"] == "publication"


def test_merge_pass_folds_split_stories(db):
    from app.pipeline.cluster import merge_duplicate_stories
    from app.pipeline.fake_llm import FakeLLM

    with conn() as c:
        s1 = c.execute("insert into stories (headline, keywords, article_count) values "
                       "('Hackers breach Australian telco customer database', '{australian,telco,breach,customers}', 1) returning id").fetchone()["id"]
        s2 = c.execute("insert into stories (headline, keywords, article_count) values "
                       "('Telco confirms breach of customer database', '{telco,breach,customers,records}', 1) returning id").fetchone()["id"]
        for sid, url in ((s1, "https://a.example/breach"), (s2, "https://b.example/breach")):
            c.execute("insert into posts (story_id, url, canonical_url, title, summary, source_name) values (%s, %s, %s, 't', 's', 'x')",
                      (sid, url, url))
    stats = {}
    merge_duplicate_stories(FakeLLM(), stats)
    assert stats.get("stories_merged", 0) >= 1
    assert one("select count(*) as n from stories where id = %s", s2)["n"] == 0
    assert one("select count(*) as n from coverage where url = 'https://b.example/breach'")["n"] == 1
    assert one("select article_count from stories where id = %s", s1)["article_count"] == 2
    with conn() as c:  # leave the shared test database as the API tests expect it
        c.execute("delete from posts where url in ('https://a.example/breach', 'https://b.example/breach')")
        c.execute("delete from coverage where url = 'https://b.example/breach'")
        c.execute("delete from stories where id = %s", (s1,))


def test_posts_use_publish_date_not_found_date(db):
    from datetime import timedelta
    from app.normalize import utcnow
    from app.pipeline.cluster import _feed_at

    now = utcnow()
    five_days_ago = now - timedelta(days=5)
    assert _feed_at(five_days_ago) == five_days_ago  # old stays old
    assert abs((_feed_at(now + timedelta(days=2)) - now).total_seconds()) < 5  # future dates are mistakes
    assert abs((_feed_at(None) - now).total_seconds()) < 5


def test_missing_dates_are_read_from_the_article(db):
    import httpx
    from datetime import timedelta
    from app.normalize import utcnow
    from app.pipeline.run import fill_missing_dates

    yesterday = (utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT08:00:00Z")
    html = f'<meta property="article:published_time" content="{yesterday}">'
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=html)))
    with conn() as c:
        cid = c.execute("""insert into candidates (topic, url, canonical_url, title, found_via)
                           values ('ai', 'https://d.example/a', 'https://d.example/a', 'x', 'search') returning id""").fetchone()["id"]
    items = [{"id": cid, "url": "https://d.example/a", "published_at": None}]
    stats = {}
    fill_missing_dates(items, stats, client)
    assert items[0]["published_at"].isoformat().startswith(yesterday[:16])
    assert stats == {"publish_dates_found": 1, "publish_dates_unknown": 0}


def test_old_articles_found_by_date_lookup_are_dropped(db):
    import httpx
    from app.pipeline.run import fill_missing_dates

    html = '<meta property="article:published_time" content="2024-01-10T08:00:00Z">'
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=html)))
    with conn() as c:
        cid = c.execute("""insert into candidates (topic, url, canonical_url, title, found_via)
                           values ('ai', 'https://d.example/old', 'https://d.example/old', 'x', 'search') returning id""").fetchone()["id"]
    items = [{"id": cid, "url": "https://d.example/old", "published_at": None}]
    stats = {}
    fill_missing_dates(items, stats, client)
    assert items == [] and stats["dropped_as_old"] == 1
    assert one("select decision from candidates where id = %s", cid)["decision"] == "rejected"


def test_follow_up_keeps_its_summary_behind_more():
    from app.pipeline.cluster import _details_for

    item = {"summary": "Root cause was a contractor account without two-factor login.", "details": "More."}
    assert _details_for(item, "OpenAI now says 2 million users were affected.") .startswith("Root cause")
    assert _details_for(item, None) == "More."


def test_links_to_private_addresses_are_not_fetched():
    from app.sources.fetch import is_public_url

    assert not is_public_url("http://127.0.0.1/admin")
    assert not is_public_url("http://192.168.1.1/")
    assert not is_public_url("file:///etc/passwd")


def test_manual_scans_wait_for_the_cooldown(db, monkeypatch):
    from app.config import settings
    from app.pipeline.run import run_scan

    monkeypatch.setattr(settings, "scan_cooldown_minutes", 30)
    assert run_scan("schedule")["status"] == "ok"  # makes sure a recent good scan exists
    skipped = run_scan("manual:joe@example.com")
    assert "skipped" in skipped and "--force" in skipped["skipped"]
    assert run_scan("schedule").get("status") == "ok"  # the schedule is never blocked
    assert run_scan("cli", force=True).get("status") == "ok"
