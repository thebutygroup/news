"""Known sources: URL classification, page watching, X, channel discovery, relevance."""
import httpx
import pytest

from app.sources.classify import classify, social_profile
from app.sources.fetch import article_links

LISTING = """<html><body><nav><a href="/news">News</a><a href="/about">About</a><a href="/news/tag/ai">AI tag</a></nav>
<a href="/news/2026/09/acme-launches-nova-3-open-weights">Acme launches Nova-3 with open weights</a>
<a href="/news/acme-partners-with-city-transit-on-screens">Acme partners with city transit</a>
<a href="https://other.example/news/something-else-entirely">Elsewhere</a>
<a href="/careers">Careers</a>{extra}</body></html>"""


@pytest.mark.parametrize("url,expected", [
    ("https://twitter.com/OpenAI", ("x", "x", "https://x.com/openai")),
    ("https://x.com/intent/tweet?text=hi", None),
    ("https://www.linkedin.com/company/Salesforce/posts/", ("linkedin", "none", "https://www.linkedin.com/company/salesforce")),
    ("https://acme.substack.com/p/some-post", ("substack", "rss", "https://acme.substack.com/feed")),
    ("https://bsky.app/profile/acme.bsky.social", ("bluesky", "rss", "https://bsky.app/profile/acme.bsky.social/rss")),
    ("https://www.anthropic.com/news", ("newsroom", "page", "https://www.anthropic.com/news")),
    ("https://openai.com/news/rss.xml", ("newsroom", "rss", "https://openai.com/news/rss.xml")),
    ("https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=x", ("blog", "rss", None)),
])
def test_classify(url, expected):
    if expected is None:
        assert social_profile(url) is None
        return
    got = classify(url)
    assert got[:2] == expected[:2]
    if expected[2]:
        assert got[2] == expected[2]


def test_article_links_skip_nav_and_other_sites():
    links = article_links(LISTING.format(extra=""), "https://acme.example/news")
    assert [u for u, _ in links] == [
        "https://acme.example/news/2026/09/acme-launches-nova-3-open-weights",
        "https://acme.example/news/acme-partners-with-city-transit-on-screens",
    ]


def _org_with_source(c, url, kind, channel):
    org = c.execute("""insert into orgs (slug, name, kind, homepage, status) values (%s, %s, 'company', %s, 'following')
                       on conflict (slug) do update set status = 'following' returning id""",
                    (f"t-{channel}", f"Test {channel}", "https://acme.example")).fetchone()["id"]
    return c.execute("""insert into sources (name, url, kind, channel, tier, org_id, origin)
                        values (%s, %s, %s, %s, 2, %s, 'added') returning *""",
                     (f"Test {channel}", url, kind, channel, org)).fetchone()


def test_page_source_baselines_then_reports_new_links(db):
    from app.db import conn
    from app.normalize import utcnow
    from app.sources.fetch import fetch_page
    from datetime import timedelta

    state = {"extra": ""}

    def handler(req):
        if req.url.path == "/news":
            return httpx.Response(200, text=LISTING.format(extra=state["extra"]))
        return httpx.Response(200, text='<meta property="og:title" content="Acme opens a London lab">'
                                        f'<meta property="article:published_time" content="{utcnow().isoformat()}">')

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with conn() as c:
        src = _org_with_source(c, "https://acme.example/news", "page", "newsroom")
    since = utcnow() - timedelta(hours=36)
    assert fetch_page(src, since, client) == []  # first look is a baseline
    state["extra"] = '<a href="/news/2026/09/acme-opens-london-lab">Acme opens a London lab</a>'
    items = fetch_page(src, since, client)
    assert [i["title"] for i in items] == ["Acme opens a London lab"]
    assert items[0]["found_via"] == "page" and items[0]["published_at"]
    assert fetch_page(src, since, client) == []  # already seen


def test_x_reader_uses_article_link_and_saves_cursor(db, monkeypatch):
    from app.config import settings
    from app.db import conn
    from app.normalize import utcnow
    from app.sources.xapi import XBudget, fetch_x

    monkeypatch.setattr(settings, "x_bearer_token", "token")
    calls = []

    def handler(req):
        calls.append(req.url.path)
        assert req.headers["authorization"] == "Bearer token"
        if req.url.path.endswith("/users/by/username/acmelabs"):
            return httpx.Response(200, json={"data": {"id": "42"}})
        return httpx.Response(200, json={"data": [{
            "id": "900", "created_at": "2026-09-24T09:00:00Z",
            "text": "Nova-3 is out today https://t.co/abc",
            "entities": {"urls": [{"url": "https://t.co/abc", "expanded_url": "https://acme.example/news/nova-3"}]},
        }], "meta": {"newest_id": "900"}})

    with conn() as c:
        src = _org_with_source(c, "https://x.com/acmelabs", "x", "x")
    budget = XBudget(50)
    items = fetch_x(src, utcnow(), httpx.Client(transport=httpx.MockTransport(handler)), budget)
    assert items[0]["url"] == "https://acme.example/news/nova-3"
    assert items[0]["title"] == "Nova-3 is out today https://acme.example/news/nova-3"
    assert budget.used == 1
    with conn() as c:
        row = c.execute("select external_id, cursor from sources where id = %s", (src["id"],)).fetchone()
    assert row == {"external_id": "42", "cursor": "900"}


def test_channel_discovery_reads_the_orgs_own_site(db):
    from app.db import conn
    from app.sources.channels import discover_for_org

    rss = ('<?xml version="1.0"?><rss version="2.0"><channel><title>Acme</title>'
           '<item><title>Post</title><link>https://acme.example/blog/post-one-here</link></item></channel></rss>')
    home = """<html><head><link rel="alternate" type="application/rss+xml" href="/blog/rss.xml"></head><body>
      <a href="/newsroom">Newsroom</a>
      <footer><a href="https://twitter.com/AcmeHQ">X</a><a href="https://www.linkedin.com/company/acme-hq/">LinkedIn</a>
      <a href="https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv">YouTube</a>
      <a href="https://twitter.com/intent/tweet?text=share">Share</a></footer></body></html>"""
    newsroom = "<html><body>" + "".join(
        f'<a href="/newsroom/2026/09/press-release-number-{i}">Release {i}</a>' for i in range(6)) + "</body></html>"

    def handler(req):
        path = req.url.path
        if path in ("/", ""):
            return httpx.Response(200, text=home)
        if path == "/blog/rss.xml":
            return httpx.Response(200, text=rss)
        if path == "/newsroom":
            return httpx.Response(200, text=newsroom)
        return httpx.Response(404)

    with conn() as c:
        org = c.execute("""insert into orgs (slug, name, kind, homepage, status)
                           values ('acme-hq', 'Acme HQ', 'company', 'https://acme.example/', 'following') returning id""").fetchone()["id"]
    result = discover_for_org(org, httpx.Client(transport=httpx.MockTransport(handler)))
    got = {(a["channel"], a["kind"]) for a in result["added"]}
    assert ("blog", "rss") in got
    assert ("x", "x") in got
    assert ("linkedin", "none") in got
    assert ("youtube", "rss") in got
    assert ("newsroom", "page") in got
    assert result["error"] is None


def test_frequently_mentioned_entities_become_proposed_orgs(db):
    from app.db import conn
    from app.pipeline.cluster import attach_tag, ensure_tag
    from app.sources.relevance import propose_from_mentions

    with conn() as c:
        tag = ensure_tag(c, "perplexity", "Perplexity", "entity")
        for i in range(3):
            pid = c.execute("""insert into posts (url, canonical_url, title, summary) values (%s, %s, %s, 's')
                               returning id""", (f"https://www.perplexity.ai/hub/p{i}", f"https://perplexity.ai/hub/p{i}",
                                                  f"Perplexity thing {i}")).fetchone()["id"]
            attach_tag(c, pid, tag)
    assert "Perplexity" in propose_from_mentions()
    with conn() as c:
        org = c.execute("select status, homepage, created_by from orgs where slug = 'perplexity'").fetchone()
    assert org == {"status": "proposed", "homepage": "https://perplexity.ai", "created_by": "relevance"}
