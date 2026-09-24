from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "ref", "ref_src", "ref_url",
    "cmpid", "cmp", "ncid", "sr_share", "igshid", "smid", "cid", "src", "source", "via",
    "share", "s", "t", "guccounter", "guce_referrer", "guce_referrer_sig", "trk", "spm",
}
TRACKING_PREFIXES = ("utm_", "mkt_", "pk_", "hsa_", "oly_", "__s")
KEEP_QUERY_HOSTS = {"youtube.com", "news.ycombinator.com", "reddit.com"}  # query string is the identity


def domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def canonical_url(url: str) -> str:
    """Normalise a URL so the same article found via two routes collapses to one key."""
    url = (url or "").strip()
    parts = urlsplit(url)
    scheme = "https" if parts.scheme in ("http", "https", "") else parts.scheme
    host = domain(url)
    if host.startswith("m.") and host.count(".") >= 2:
        host = host[2:]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    if host.endswith("youtube.com") and path == "/watch":
        query = [(k, v) for k, v in parse_qsl(parts.query) if k == "v"]
    elif host == "youtu.be":
        host, query, path = "youtube.com", [("v", path.strip("/"))], "/watch"
    else:
        keep_all = any(host.endswith(h) for h in KEEP_QUERY_HOSTS)
        query = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=False)
            if keep_all or (k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES))
        ]
    query.sort()
    return urlunsplit((scheme, host, path, urlencode(query), ""))


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(value: str | None, limit: int = 600) -> str:
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


def slugify(value: str, limit: int = 60) -> str:
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    value = re.sub(r"-{2,}", "-", value)
    return value[:limit].strip("-")


def title_tokens(value: str) -> set[str]:
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are", "its", "at", "by", "from", "as", "new", "how"}
    return {t for t in re.findall(r"[a-z0-9]+", (value or "").lower()) if t not in stop and len(t) > 1}


def jaccard(a: str, b: str) -> float:
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
