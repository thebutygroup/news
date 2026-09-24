"""Who is making this request.

No login code. The site sits behind Cloudflare Access, which authenticates people and
forwards a signed JWT in the Cf-Access-Jwt-Assertion header. We verify that JWT and read
the email from it. At Bauer, swap Cloudflare for whatever SSO proxy sits in front and change
only this file.

Public reading, signed-in writing: put the Access application on the /login path only. Anyone can
read the site. Visiting /login makes Access sign you in and set its CF_Authorization cookie for the
whole hostname, and every later request carries that cookie, so votes, comments and tags know who
you are. The token is verified here either way, from the header or the cookie.

Modes, in priority order:
  1. CF_ACCESS_TEAM_DOMAIN + CF_ACCESS_AUD set: verify the JWT (recommended).
  2. TRUST_CF_EMAIL_HEADER=1: trust Cf-Access-Authenticated-User-Email as-is. Only safe when
     the container port is not reachable except through the tunnel.
  3. DEV_USER_EMAIL set: everyone is this user. Local development only.
Otherwise the site is read-only.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import jwt
from fastapi import Request

from .config import settings

log = logging.getLogger("news.auth")


@lru_cache(maxsize=1)
def _jwks_client() -> jwt.PyJWKClient:
    team = settings.cf_access_team_domain.rstrip("/")
    if not team.startswith("http"):
        team = f"https://{team}"
    return jwt.PyJWKClient(f"{team}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600)


def _verify_cf_jwt(token: str) -> str | None:
    try:
        key = _jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=settings.cf_access_aud)
    except Exception as exc:  # noqa: BLE001 - any failure means "not authenticated"
        log.warning("Cloudflare Access JWT rejected: %s", exc)
        return None
    email = claims.get("email")
    return email.lower() if isinstance(email, str) else None


def auth_mode() -> str:
    if settings.cf_access_team_domain and settings.cf_access_aud:
        return "cloudflare-jwt"
    if settings.trust_cf_email_header:
        return "cloudflare-header"
    if settings.dev_user_email:
        return "dev"
    return "read-only"


def current_user(request: Request) -> str | None:
    mode = auth_mode()
    if mode == "cloudflare-jwt":
        token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization")
        return _verify_cf_jwt(token) if token else None
    if mode == "cloudflare-header":
        email = request.headers.get("cf-access-authenticated-user-email")
        return email.lower() if email else None
    if mode == "dev":
        return settings.dev_user_email.lower()
    return None


def is_admin(email: str | None) -> bool:
    if not email:
        return False
    if auth_mode() == "dev" and not settings.admin_emails:
        return True
    return email.lower() in settings.admin_emails


def display_name(email: str) -> str:
    local = email.split("@", 1)[0]
    return local.replace(".", " ").replace("_", " ").title()
