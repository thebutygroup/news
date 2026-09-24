"""What kind of channel is this URL, and how do we read it?

    classify("https://x.com/OpenAI")                  -> ("x", "x", "https://x.com/openai")
    classify("https://acme.substack.com")             -> ("substack", "rss", "https://acme.substack.com/feed")
    classify("https://www.anthropic.com/news")        -> ("newsroom", "page", ...)

The read mode ("kind" in the sources table) is one of:
    rss   fetch and parse a feed (RSS, Atom, Substack, YouTube, Bluesky, Mastodon, GitHub releases)
    page  fetch an HTML listing page and watch for new article links
    x     read via the X API, only if X_BEARER_TOKEN is set
    none  stored as a link for people to click; covered by web search, not polled
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from ..normalize import domain

FEED_HINT = re.compile(r"(\.(xml|rss|atom)(\?|$)|/feed/?(\?|$)|/rss/?(\?|$)|/atom/?(\?|$)|/feeds?/|[?&]format=rss|feedburner\.com)", re.I)
NEWSROOM_HINT = re.compile(r"/(news|newsroom|press|press-releases|media-centre|media-center|announcements)(/|$)", re.I)
RESEARCH_HINT = re.compile(r"/(research|papers|publications)(/|$)", re.I)

SOCIAL_HOSTS = {
    "x.com": "x", "twitter.com": "x",
    "linkedin.com": "linkedin",
    "youtube.com": "youtube",
    "github.com": "github",
    "bsky.app": "bluesky",
    "threads.net": "threads", "threads.com": "threads",
    "instagram.com": "instagram",
}
X_RESERVED = {"intent", "share", "home", "search", "i", "hashtag", "explore", "settings", "login", "signup", "tos", "privacy"}


def social_profile(url: str) -> tuple[str, str] | None:
    """(channel, normalised profile URL) for a social profile link, or None for share buttons and posts."""
    parts = urlsplit(url)
    host = domain(url)
    channel = SOCIAL_HOSTS.get(host)
    segs = [s for s in parts.path.split("/") if s]
    if host.endswith(".substack.com") and host != "substack.com":
        return "substack", f"https://{host}"
    if not channel or not segs:
        return None
    first = segs[0]
    if channel == "x":
        if len(segs) != 1 or first.lower() in X_RESERVED:
            return None
        return "x", f"https://x.com/{first.lower()}"
    if channel == "linkedin":
        if len(segs) >= 2 and first in ("company", "showcase", "school"):
            return "linkedin", f"https://www.linkedin.com/{first}/{segs[1].lower()}"
        return None
    if channel == "youtube":
        if first.startswith("@") and len(segs) <= 2:
            return "youtube", f"https://www.youtube.com/{first.lower()}"
        if first in ("channel", "c", "user") and len(segs) >= 2:
            return "youtube", f"https://www.youtube.com/{first}/{segs[1]}"
        return None
    if channel == "github":
        if len(segs) == 1 and first.lower() not in ("features", "about", "pricing", "login", "orgs", "sponsors"):
            return "github", f"https://github.com/{first.lower()}"
        if len(segs) >= 2 and first == "orgs":
            return "github", f"https://github.com/{segs[1].lower()}"
        return None
    if channel == "bluesky":
        if len(segs) >= 2 and first == "profile":
            return "bluesky", f"https://bsky.app/profile/{segs[1].lower()}"
        return None
    if channel in ("threads", "instagram"):
        if len(segs) == 1 and first not in ("p", "reel", "explore", "accounts"):
            handle = first.lstrip("@").lower()
            return channel, (f"https://www.threads.net/@{handle}" if channel == "threads" else f"https://www.instagram.com/{handle}")
        return None
    return None


def classify(url: str, channel_hint: str | None = None) -> tuple[str, str, str]:
    """(channel, read mode, URL to store)."""
    url = url.strip()
    social = social_profile(url)
    if social:
        channel, profile = social
        if channel == "substack":
            return "substack", "rss", f"{profile}/feed"
        if channel == "bluesky":
            return "bluesky", "rss", f"{profile}/rss"
        if channel == "x":
            return "x", "x", profile
        return channel, "none", profile
    host = domain(url)
    if host == "youtube.com" and "feeds/videos.xml" in url:
        return "youtube", "rss", url
    if host.endswith(".substack.com") and url.rstrip("/").endswith("/feed"):
        return "substack", "rss", url
    if "news.ycombinator.com" in host:
        return "community", "none", url
    if FEED_HINT.search(url):
        return channel_hint or _guess_channel(url), "rss", url
    return channel_hint or _guess_channel(url), "page", url


def _guess_channel(url: str) -> str:
    path = urlsplit(url).path
    if NEWSROOM_HINT.search(path):
        return "newsroom"
    if RESEARCH_HINT.search(path):
        return "research"
    if "podcast" in url.lower():
        return "podcast"
    return "blog"


# ---- HTML parsing -----------------------------------------------------------
class PageParser(HTMLParser):
    """Collects anchors, feed links and the handful of meta tags we care about."""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.links: list[tuple[str, str]] = []
        self.feeds: list[str] = []
        self.meta: dict[str, str] = {}
        self.title = ""
        self.times: list[str] = []
        self._in_a: str | None = None
        self._a_text: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and a.get("href"):
            self._in_a = urljoin(self.base, a["href"].strip())
            self._a_text = [a.get("aria-label", "")] if a.get("aria-label") else []
        elif tag == "link" and "alternate" in a.get("rel", "").lower() and re.search(r"(rss|atom)\+xml", a.get("type", ""), re.I):
            if a.get("href"):
                self.feeds.append(urljoin(self.base, a["href"].strip()))
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key in ("og:title", "og:description", "description", "article:published_time", "datepublished",
                       "og:site_name", "twitter:title", "twitter:description", "parsely-pub-date"):
                self.meta.setdefault(key, a.get("content", ""))
        elif tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            text = re.sub(r"\s+", " ", " ".join(self._a_text)).strip()
            self.links.append((self._in_a, text))
            self._in_a = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_a is not None:
            self._a_text.append(data)
        if self._in_title:
            self.title += data


def parse_html(html: str, base_url: str) -> PageParser:
    parser = PageParser(base_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - a half-parsed page is still useful
        pass
    return parser


def site_key(url: str) -> str:
    """Registrable-ish domain: example.com, example.co.uk. Good enough to tell same-site links apart."""
    parts = domain(url).split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "gov", "ac", "net") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
