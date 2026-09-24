from __future__ import annotations

import hashlib
import logging
import re
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import queries
from .auth import auth_mode, current_user, display_name, is_admin
from .config import load_topics, settings
from .db import conn
from .normalize import parse_iso, slugify
from .pipeline.cluster import attach_tag, ensure_tag
from .sources.channels import discover_for_org
from .sources.classify import classify
from .seed import bootstrap

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("news.web")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    bootstrap()
    if auth_mode() == "cloudflare-header":
        log.warning("Trusting the Cloudflare email header without JWT verification. "
                    "Set CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD to verify it.")
    if auth_mode() == "read-only":
        log.warning("No identity configured, the site is read-only. See README, 'Identity'.")
    yield


app = FastAPI(title="News", lifespan=lifespan, docs_url=None, redoc_url=None)


# -- guards ----------------------------------------------------------------
@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """Reject cross-site writes. The Access cookie would otherwise ride along on them."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        origin_host = urlsplit(origin).netloc.lower() if origin else ""
        if origin and origin_host != (host or "").lower() and origin_host not in settings.allowed_hosts:
            return JSONResponse({"detail": "cross-origin write refused"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    """Pages and scripts are revalidated on every visit, so a deploy shows up straight away,
    including through Cloudflare's cache. Fonts never change, so they're cached for a year."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/fonts/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.startswith("/static/") or not path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


def user(request: Request) -> str | None:
    return current_user(request)


VOTER_COOKIE = "news_voter"


def voter(request: Request) -> tuple[str | None, str | None]:
    """Who is voting: the signed-in email, or an anonymous id kept in a cookie so a guest's vote
    can be undone. Returns (voter id, new cookie value if one needs setting)."""
    email = current_user(request)
    if email:
        return email, None
    token = request.cookies.get(VOTER_COOKIE, "")
    if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token):
        return f"guest:{token}", None
    return None, secrets.token_urlsafe(18)


def require_user(request: Request) -> str:
    email = current_user(request)
    if not email:
        raise HTTPException(401, "Sign in through the company login to vote, comment or tag.")
    return email


def require_admin(request: Request) -> str:
    email = require_user(request)
    if not is_admin(email):
        raise HTTPException(403, "Admins only. Add your email to ADMIN_EMAILS.")
    return email


def _post_exists(c, post_id: int) -> None:
    if not c.execute("select 1 from posts where id = %s", (post_id,)).fetchone():
        raise HTTPException(404, "Post not found")


# -- pages -----------------------------------------------------------------
def _static_version() -> str:
    digest = hashlib.sha1()
    for path in sorted(settings.static_dir.glob("*")):
        if path.suffix in (".js", ".css"):
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


STATIC_VERSION = _static_version()


def page(name: str) -> HTMLResponse:
    """Serve a page with ?v=<hash of the scripts> on its script and style links, so browsers and
    Cloudflare fetch new code after every deploy instead of running a cached copy."""
    html = (settings.static_dir / name).read_text()
    html = html.replace('.js"', f'.js?v={STATIC_VERSION}"').replace('.css"', f'.css?v={STATIC_VERSION}"')
    return HTMLResponse(html)


@app.get("/", include_in_schema=False)
def index():
    return page("index.html")


@app.get("/runs", include_in_schema=False)
def runs_page():
    return page("runs.html")


@app.get("/login", include_in_schema=False)
def login(next: str = "/"):
    """Cloudflare Access protects only this path. Reaching it means you signed in; Access has now set
    its cookie for the whole site, so send you back to where you were."""
    safe = next if next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(safe, status_code=303)


@app.get("/logout", include_in_schema=False)
def logout():
    return RedirectResponse("/cdn-cgi/access/logout", status_code=303)


@app.get("/sources", include_in_schema=False)
def sources_page():
    return page("sources.html")


app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")


@app.get("/healthz")
def healthz():
    with conn() as c:
        c.execute("select 1")
    return {"ok": True}


# -- read ------------------------------------------------------------------
@app.get("/api/me")
def me(email: str | None = Depends(user)):
    return {"email": email, "name": display_name(email) if email else None,
            "admin": is_admin(email), "auth_mode": auth_mode()}


@app.get("/api/feed")
def get_feed(request: Request, t: list[str] = Query(default=[]), q: str | None = None,
             sort: str | None = None, before: str | None = None, limit: int = 120):
    cursor = parse_iso(before) if before else None
    who, _ = voter(request)  # guests see their own upvotes highlighted too
    return queries.feed(who, t, q, sort, cursor, limit)


@app.get("/api/tags")
def get_tags(prefix: str = "", limit: int = 12):
    return queries.suggest_tags(prefix, min(limit, 30))


@app.get("/api/meta")
def get_meta():
    topics = load_topics()
    with conn() as c:
        last = c.execute("select id, finished_at, status from runs where finished_at is not null order by id desc limit 1").fetchone()
    return {
        "topics": [{"slug": t.slug, "label": t.label} for t in topics],
        "similar": [s for t in topics for s in t.similar],
        "last_run": last,
        "schedule": settings.scan_cron,
        "x_enabled": bool(settings.x_bearer_token),
        "timezone": settings.timezone,
    }


@app.get("/api/posts/{post_id}/coverage")
def get_coverage(post_id: int):
    with conn() as c:
        return c.execute(
            """select title, url, source_name, published_at from coverage where post_id = %s
               order by coalesce(published_at, created_at)""",
            (post_id,),
        ).fetchall()


@app.get("/api/stories/{story_id}")
def get_story(story_id: int):
    with conn() as c:
        story = c.execute("select id, headline, article_count, first_seen_at, last_update_at from stories where id = %s",
                          (story_id,)).fetchone()
        if not story:
            raise HTTPException(404, "Story not found")
        story["posts"] = c.execute(
            "select id, title, delta, source_name, url, feed_at from posts where story_id = %s order by feed_at",
            (story_id,),
        ).fetchall()
    return story


@app.get("/api/posts/{post_id}/comments")
def get_comments(post_id: int):
    with conn() as c:
        rows = c.execute(
            "select id, user_email, body, created_at from comments where post_id = %s order by created_at", (post_id,)
        ).fetchall()
    return [{**r, "name": display_name(r["user_email"])} for r in rows]


# -- write -----------------------------------------------------------------
@app.post("/api/posts/{post_id}/vote")
def toggle_vote(post_id: int, request: Request, response: Response):
    """Anyone can upvote. Signed-in votes count as you; guests get an anonymous cookie so they can
    take a vote back. Clearing cookies lets a guest vote again, which is fine for a small team feed."""
    who, new_token = voter(request)
    if new_token:
        who = f"guest:{new_token}"
        response.set_cookie(VOTER_COOKIE, new_token, max_age=365 * 24 * 3600, httponly=True,
                            secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
                            samesite="lax")
    with conn() as c:
        _post_exists(c, post_id)
        removed = c.execute("delete from votes where post_id = %s and user_email = %s returning 1", (post_id, who)).fetchone()
        if not removed:
            c.execute("insert into votes (post_id, user_email) values (%s, %s)", (post_id, who))
        count = c.execute("select count(*) as n from votes where post_id = %s", (post_id,)).fetchone()["n"]
    return {"voted": not removed, "votes": count}


@app.post("/api/posts/{post_id}/not-relevant")
def toggle_not_relevant(post_id: int, email: str = Depends(require_user)):
    with conn() as c:
        _post_exists(c, post_id)
        removed = c.execute(
            "delete from feedback where post_id = %s and user_email = %s and kind = 'not_relevant' returning 1",
            (post_id, email),
        ).fetchone()
        if not removed:
            c.execute("insert into feedback (post_id, user_email, kind) values (%s, %s, 'not_relevant')", (post_id, email))
    return {"flagged": not removed}


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


@app.post("/api/posts/{post_id}/comments")
def add_comment(post_id: int, data: CommentIn, email: str = Depends(require_user)):
    body = data.body.strip()
    if not body:
        raise HTTPException(422, "Write something first.")
    with conn() as c:
        _post_exists(c, post_id)
        row = c.execute(
            "insert into comments (post_id, user_email, body) values (%s, %s, %s) returning id, user_email, body, created_at",
            (post_id, email, body),
        ).fetchone()
    return {**row, "name": display_name(email)}


@app.delete("/api/comments/{comment_id}")
def delete_comment(comment_id: int, email: str = Depends(require_user)):
    with conn() as c:
        row = c.execute("select user_email from comments where id = %s", (comment_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Comment not found")
        if row["user_email"] != email and not is_admin(email):
            raise HTTPException(403, "You can only delete your own comments.")
        c.execute("delete from comments where id = %s", (comment_id,))
    return {"deleted": True}


class TagIn(BaseModel):
    tag: str = Field(min_length=1, max_length=60)


@app.post("/api/posts/{post_id}/tags")
def add_tag(post_id: int, data: TagIn, email: str = Depends(require_user)):
    slug = slugify(data.tag, 40)
    if len(slug) < 2:
        raise HTTPException(422, "Tags need at least two letters or numbers.")
    with conn() as c:
        _post_exists(c, post_id)
        existing = c.execute("select id, slug, label, type from tags where slug = %s", (slug,)).fetchone()
        if existing:
            tag_id = existing["id"]
        else:
            tag_id = ensure_tag(c, slug, data.tag.strip()[:60], "user", created_by=email)
        attach_tag(c, post_id, tag_id, added_by=email)
        tag = c.execute("select slug, label, type from tags where id = %s", (tag_id,)).fetchone()
    return {**tag, "added_by": email}


@app.delete("/api/posts/{post_id}/tags/{slug}")
def remove_tag(post_id: int, slug: str, email: str = Depends(require_user)):
    with conn() as c:
        row = c.execute(
            """select pt.added_by, t.id from post_tags pt join tags t on t.id = pt.tag_id
               where pt.post_id = %s and t.slug = %s""",
            (post_id, slug),
        ).fetchone()
        if not row:
            raise HTTPException(404, "That tag is not on this post.")
        if row["added_by"] != email and not is_admin(email):
            raise HTTPException(403, "You can only remove tags you added.")
        c.execute("delete from post_tags where post_id = %s and tag_id = %s", (post_id, row["id"]))
    return {"removed": True}


# -- admin: runs, sources, hide --------------------------------------------
@app.get("/api/runs")
def list_runs(limit: int = 20):
    with conn() as c:
        return c.execute("select id, trigger, started_at, finished_at, status, stats, error from runs order by id desc limit %s",
                         (min(limit, 100),)).fetchall()


@app.get("/api/runs/{run_id}/candidates")
def run_candidates(run_id: int, decision: str | None = None):
    sql = """select id, title, url, source_name, found_via, tier, decision, reason, post_id, published_at
             from candidates where run_id = %s"""
    params: list = [run_id]
    if decision:
        sql += " and decision = %s"
        params.append(decision)
    sql += " order by decision, id"
    with conn() as c:
        return c.execute(sql, params).fetchall()


# -- known sources: organisations and their channels -----------------------
ORG_SQL = """
select o.id, o.slug, o.name, o.kind, o.homepage, o.priority, o.status, o.topics, o.aliases, o.notes,
       o.mention_count, o.search_hits, o.last_mentioned_at, o.channels_discovered_at, o.created_by,
       t.slug as tag_slug,
       coalesce((select json_agg(json_build_object(
           'id', s.id, 'url', s.url, 'channel', s.channel, 'kind', s.kind, 'status', s.status, 'tier', s.tier,
           'origin', s.origin, 'fail_count', s.fail_count, 'last_error', s.last_error,
           'last_fetched_at', s.last_fetched_at, 'last_new_item_at', s.last_new_item_at, 'verified_at', s.verified_at,
           'found_30d', (select count(*) from candidates c where c.source_id = s.id and c.created_at > now() - interval '30 days'),
           'posted_30d', (select count(*) from candidates c where c.source_id = s.id and c.decision = 'post'
                          and c.created_at > now() - interval '30 days'))
         order by s.kind = 'none', s.channel) from sources s where s.org_id = o.id and s.status <> 'dismissed'), '[]') as channels,
       (select count(*) from candidates c where c.org_id = o.id and c.created_at > now() - interval '30 days') as found_30d,
       (select count(*) from candidates c where c.org_id = o.id and c.decision = 'post'
          and c.created_at > now() - interval '30 days') as posted_30d,
       coalesce((select json_agg(x) from (select p.id, p.title, p.url from posts p join post_tags pt on pt.post_id = p.id
          where pt.tag_id = o.tag_id order by p.feed_at desc limit 3) x), '[]') as recent_posts
from orgs o left join tags t on t.id = o.tag_id
"""


@app.get("/api/orgs")
def list_orgs(status: str = Query("following", pattern="^(following|proposed|dismissed)$")):
    where = "where o.status = %s"
    params: list = [status]
    if status == "proposed":
        where += " and (o.mention_count >= %s or o.search_hits >= %s)"
        params += [settings.org_propose_threshold, settings.proposed_source_threshold]
    order = ("order by greatest(o.mention_count, o.search_hits) desc, o.name" if status == "proposed"
             else "order by o.kind, o.priority, o.name")
    with conn() as c:
        return c.execute(f"{ORG_SQL} {where} {order}", params).fetchall()


@app.get("/api/sources/topic")
def topic_sources():
    """Feeds that belong to a topic rather than an organisation: Hacker News, subreddits, government search feeds."""
    with conn() as c:
        return c.execute(
            """select s.id, s.topic, s.name, s.url, s.kind, s.channel, s.status, s.tier, s.fail_count, s.last_error,
                      s.last_fetched_at, s.last_new_item_at,
                      (select count(*) from candidates c where c.source_id = s.id and c.created_at > now() - interval '30 days') as found_30d,
                      (select count(*) from candidates c where c.source_id = s.id and c.decision = 'post'
                         and c.created_at > now() - interval '30 days') as posted_30d
               from sources s where s.org_id is null and s.status <> 'dismissed' order by s.topic, s.name"""
        ).fetchall()


class OrgIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    homepage: str | None = None
    kind: str = Field(default="company", pattern="^(company|lab|regulator|publication|person|community)$")
    priority: str = Field(default="core", pattern="^(core|watch)$")
    topics: list[str] = ["ai"]


@app.post("/api/orgs")
def create_org(data: OrgIn, email: str = Depends(require_admin)):
    slug = slugify(data.name, 50)
    with conn() as c:
        tag_id = ensure_tag(c, slug, data.name.strip(), "entity")
        row = c.execute(
            """insert into orgs (slug, name, kind, homepage, priority, status, topics, tag_id, created_by)
               values (%s, %s, %s, %s, %s, 'following', %s, %s, %s)
               on conflict (slug) do update set status = 'following', homepage = coalesce(excluded.homepage, orgs.homepage)
               returning id""",
            (slug, data.name.strip(), data.kind, data.homepage, data.priority, data.topics, tag_id, email),
        ).fetchone()
    return {"id": row["id"], "discovery": discover_for_org(row["id"]) if data.homepage else None}


class FollowIn(BaseModel):
    homepage: str | None = None
    kind: str | None = Field(default=None, pattern="^(company|lab|regulator|publication|person|community)$")


@app.post("/api/orgs/{org_id}/follow")
def follow_org(org_id: int, data: FollowIn, _admin: str = Depends(require_admin)):
    with conn() as c:
        row = c.execute(
            """update orgs set status = 'following', homepage = coalesce(%s, homepage), kind = coalesce(%s, kind)
               where id = %s returning id, homepage""",
            ((data.homepage or "").strip() or None, data.kind, org_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Organisation not found")
    return {"following": True, "discovery": discover_for_org(org_id) if row["homepage"] else None}


@app.post("/api/orgs/{org_id}/status")
def set_org_status(org_id: int, status: str = Query(pattern="^(following|dismissed)$"),
                   _admin: str = Depends(require_admin)):
    with conn() as c:
        c.execute("update orgs set status = %s where id = %s", (status, org_id))
    return {"status": status}


@app.post("/api/orgs/{org_id}/priority")
def set_org_priority(org_id: int, value: str = Query(pattern="^(core|watch)$"), _admin: str = Depends(require_admin)):
    hours = settings.watch_check_hours if value == "watch" else 0
    with conn() as c:
        c.execute("update orgs set priority = %s where id = %s", (value, org_id))
        c.execute("update sources set check_every_hours = %s where org_id = %s", (hours, org_id))
    return {"priority": value}


@app.post("/api/orgs/{org_id}/discover")
def rediscover(org_id: int, _admin: str = Depends(require_admin)):
    return discover_for_org(org_id)


class ChannelIn(BaseModel):
    url: str = Field(min_length=8, max_length=1000)
    channel: str | None = None


@app.post("/api/orgs/{org_id}/channels")
def add_channel(org_id: int, data: ChannelIn, _admin: str = Depends(require_admin)):
    url = data.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(422, "Paste a full link starting with https://")
    channel, kind, stored = classify(url, data.channel)
    with conn() as c:
        org = c.execute("select * from orgs where id = %s", (org_id,)).fetchone()
        if not org:
            raise HTTPException(404, "Organisation not found")
        row = c.execute(
            """insert into sources (topic, name, url, homepage, kind, channel, tier, org_id, origin, check_every_hours)
               values (null, %s, %s, %s, %s, %s, %s, %s, 'added', %s)
               on conflict (url) do update set org_id = excluded.org_id, status = 'active'
               returning id, url, channel, kind""",
            (f"{org['name']} {channel}", stored, org["homepage"], kind, channel, org["default_tier"], org_id,
             settings.watch_check_hours if org["priority"] == "watch" else 0),
        ).fetchone()
    return row


@app.post("/api/sources/{source_id}/status")
def set_source_status(source_id: int, status: str = Query(pattern="^(active|paused|dismissed)$"),
                      _admin: str = Depends(require_admin)):
    with conn() as c:
        c.execute("update sources set status = %s, fail_count = 0 where id = %s", (status, source_id))
    return {"status": status}


@app.post("/api/scan")
def request_scan(email: str = Depends(require_admin)):
    with conn() as c:
        pending = c.execute("select id from scan_requests where picked_up_at is null").fetchone()
        if not pending:
            c.execute("insert into scan_requests (requested_by) values (%s)", (email,))
    return {"queued": True}


@app.post("/api/posts/{post_id}/hide")
def hide_post(post_id: int, _admin: str = Depends(require_admin)):
    with conn() as c:
        c.execute("update posts set hidden = not hidden where id = %s", (post_id,))
    return {"ok": True}

