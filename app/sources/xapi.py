"""X accounts, read through the official API. Optional.

Off unless X_BEARER_TOKEN is set; without it, X channels are stored as links and web search
covers them. The API is pay per use, billed per post returned, so we:
  - look each account's user id up once and cache it (sources.external_id)
  - ask only for posts newer than the last one we saw (sources.cursor, the since_id)
  - skip replies and reposts
  - stop at X_MAX_READS_PER_RUN posts per scan
If a post links to an article, the article URL becomes the candidate, so it dedups against the
same article arriving by RSS.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from ..config import settings
from ..db import conn
from ..normalize import clean_text, domain, parse_iso

log = logging.getLogger("news.x")
API = "https://api.x.com/2"
TCO = re.compile(r"https://t\.co/\w+")


class XBudget:
    """Shared across one scan, so the cap covers every X source together."""

    def __init__(self, cap: int):
        self.cap, self.used = cap, 0

    def left(self) -> int:
        return max(0, self.cap - self.used)


def handle_of(url: str) -> str:
    return urlsplit(url).path.strip("/").split("/")[0]


def fetch_x(source: dict, since: datetime, client: httpx.Client, budget: XBudget) -> list[dict]:
    if not settings.x_bearer_token or budget.left() < 5:
        return []
    headers = {"Authorization": f"Bearer {settings.x_bearer_token}"}
    user_id = source.get("external_id")
    if not user_id:
        r = client.get(f"{API}/users/by/username/{handle_of(source['url'])}", headers=headers)
        r.raise_for_status()
        user_id = r.json()["data"]["id"]
        with conn() as c:
            c.execute("update sources set external_id = %s where id = %s", (user_id, source["id"]))
    params = {
        "max_results": min(20, max(5, budget.left())),
        "exclude": "replies,retweets",
        "tweet.fields": "created_at,entities",
    }
    if source.get("cursor"):
        params["since_id"] = source["cursor"]
    else:
        params["start_time"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    r = client.get(f"{API}/users/{user_id}/tweets", headers=headers, params=params)
    r.raise_for_status()
    body = r.json()
    posts = body.get("data", [])
    budget.used += len(posts)
    newest = body.get("meta", {}).get("newest_id")
    if newest:
        with conn() as c:
            c.execute("update sources set cursor = %s where id = %s", (newest, source["id"]))

    handle = handle_of(source["url"])
    items = []
    for post in posts:
        text = post.get("text", "")
        links = []
        for u in (post.get("entities") or {}).get("urls", []):
            expanded = u.get("unwound_url") or u.get("expanded_url") or ""
            if u.get("url"):
                text = text.replace(u["url"], expanded)
            if expanded and domain(expanded) not in ("x.com", "twitter.com"):
                links.append(expanded)
        text = TCO.sub("", text).strip()
        status_url = f"https://x.com/{handle}/status/{post['id']}"
        first_line = text.split("\n", 1)[0]
        items.append({
            "url": links[0] if links else status_url,
            "title": clean_text(first_line, 200) or f"Post by @{handle}",
            "excerpt": clean_text(f"{text} (posted on X: {status_url})", 600),
            "source_name": source.get("display_name") or f"@{handle} on X",
            "source_id": source["id"],
            "org_id": source.get("org_id"),
            "tier": source["tier"],
            "published_at": parse_iso(post.get("created_at")),
            "found_via": "x",
        })
    return items
