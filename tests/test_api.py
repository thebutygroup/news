import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(db):
    from app.pipeline.run import run_scan
    from app.web import app

    run_scan("test")  # harmless if the pipeline tests already ran
    with TestClient(app) as c:
        yield c


def feed(client, qs=""):
    r = client.get(f"/api/feed{qs}")
    assert r.status_code == 200, r.text
    return r.json()


def test_pages_and_health(client):
    assert client.get("/").status_code == 200
    assert client.get("/runs").status_code == 200
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/static/app.js").status_code == 200


def test_login_redirects_back_safely(client):
    r = client.get("/login?next=/sources", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sources"
    r = client.get("/login?next=//evil.example", follow_redirects=False)
    assert r.headers["location"] == "/"
    r = client.get("/logout", follow_redirects=False)
    assert r.headers["location"] == "/cdn-cgi/access/logout"


def test_me_in_dev_mode(client):
    me = client.get("/api/me").json()
    assert me["email"] == "joe@example.com" and me["admin"] is True


def test_feed_filters(client):
    everything = feed(client)["posts"]
    assert len(everything) >= 3
    assert all(any(t["slug"] == "ai" for t in p["tags"]) for p in everything)
    sec = feed(client, "?t=security")["posts"]
    assert sec and all(any(t["slug"] == "security" for t in p["tags"]) for p in sec)
    not_sec = feed(client, "?t=-security")["posts"]
    assert len(sec) + len(not_sec) == len(everything)
    unknown = feed(client, "?t=not-a-real-tag")
    assert unknown["unknown_tags"] == ["not-a-real-tag"] and len(unknown["posts"]) == len(everything)
    assert [p["title"] for p in feed(client, "?q=nova")["posts"]] == ["Acme AI releases Nova-3 open weights model"]
    assert feed(client, "?sort=top")["filters"]["sort"] == "top"


def test_tag_suggestions(client):
    got = client.get("/api/tags?prefix=legis").json()
    assert got[0]["slug"] == "legislation"


def test_vote_comment_tag_flag(client):
    post = feed(client, "?q=nova")["posts"][0]
    pid = post["id"]
    assert client.post(f"/api/posts/{pid}/vote").json() == {"voted": True, "votes": 1}
    assert client.post(f"/api/posts/{pid}/vote").json() == {"voted": False, "votes": 0}
    c = client.post(f"/api/posts/{pid}/comments", json={"body": "Worth a look"}).json()
    assert c["name"] == "Joe"
    assert len(client.get(f"/api/posts/{pid}/comments").json()) == 1
    assert client.delete(f"/api/comments/{c['id']}").json() == {"deleted": True}
    t = client.post(f"/api/posts/{pid}/tags", json={"tag": "Must read"}).json()
    assert t["slug"] == "must-read" and t["type"] == "user"
    assert [p["id"] for p in feed(client, "?t=must-read")["posts"]] == [pid]
    assert client.post(f"/api/posts/{pid}/not-relevant").json() == {"flagged": True}
    assert feed(client, "?q=nova")["posts"][0]["flagged"] is True


def test_coverage_and_story(client):
    lead = [p for p in feed(client, "?q=hugging")["posts"] if p["coverage_count"]][0]
    cov = client.get(f"/api/posts/{lead['id']}/coverage").json()
    assert cov and cov[0]["source_name"] == "Tech Wire"
    story = client.get(f"/api/stories/{lead['story_id']}").json()
    assert story["posts"]


def test_cross_origin_write_refused(client):
    pid = feed(client)["posts"][0]["id"]
    r = client.post(f"/api/posts/{pid}/vote", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_runs_and_sources(client):
    runs = client.get("/api/runs").json()
    assert runs and runs[0]["status"] == "ok"
    cands = client.get(f"/api/runs/{runs[-1]['id']}/candidates?decision=rejected").json()
    assert any(c["title"] == "Quarterly office party photos" for c in cands)
    assert client.get("/api/sources/topic").json()[0]["name"] == "Tech Wire"
    assert client.post("/api/scan").json() == {"queued": True}
    assert client.get("/sources").status_code == 200


def test_orgs_api(client):
    orgs = client.get("/api/orgs").json()
    acme = [o for o in orgs if o["slug"] == "acme-ai"][0]
    kinds = {c["channel"]: c["kind"] for c in acme["channels"]}
    assert kinds == {"blog": "rss", "x": "x"}
    assert acme["posted_30d"] >= 1 and acme["mention_count"] >= 1
    new = client.post("/api/orgs", json={"name": "Salesforce", "kind": "company"}).json()
    ch = client.post(f"/api/orgs/{new['id']}/channels", json={"url": "https://www.linkedin.com/company/Salesforce/posts"}).json()
    assert ch["kind"] == "none" and ch["url"] == "https://www.linkedin.com/company/salesforce"
    assert client.post(f"/api/orgs/{new['id']}/priority?value=watch").json() == {"priority": "watch"}
    proposed = client.get("/api/orgs?status=proposed").json()
    assert isinstance(proposed, list)
