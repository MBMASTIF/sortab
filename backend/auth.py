""""Soft" email identification — NOT a real account system.

Explicit, deliberate scope (see the task brief / README "мягкий email"):
no password, no email confirmation, no JWT. Saving a Project needs *some*
way to say "these projects belong to the same person" without putting up
a registration wall in front of a low-friction save action. The mechanism:

    POST /api/auth/identify {email} -> opaque uuid4 token, stored as
    `token -> user_id` in Redis with a 90-day TTL, handed back as an
    httpOnly cookie.

This is a bearer-token-via-cookie "browser session", not a JWT — there is
nothing to verify cryptographically, the token is just a lookup key, and
Redis is the source of truth (revoking access = deleting the Redis key).
Reuses the same Redis instance/connection convention as
backend/session_store.py (connection is a parameter, not a global).
"""

from __future__ import annotations

import os
import uuid

from redis import Redis

AUTH_COOKIE_NAME = "gruper_session"
AUTH_TOKEN_TTL_SECONDS = 90 * 24 * 60 * 60  # 90 days
AUTH_TOKEN_KEY_PREFIX = "gruper:authtoken:"

# The server currently serves plain HTTP (no domain/TLS yet — see README
# "SSL сертификат" under future work). `Secure` cookies are silently
# dropped by browsers over plain HTTP, which would break login entirely,
# so this is env-gated rather than hardcoded True. Set GRUPER_COOKIE_SECURE=1
# once HTTPS is live.
COOKIE_SECURE = os.environ.get("GRUPER_COOKIE_SECURE", "0") == "1"


def _token_key(token: str) -> str:
    return f"{AUTH_TOKEN_KEY_PREFIX}{token}"


def create_auth_token(redis_conn: Redis, user_id: str) -> str:
    token = str(uuid.uuid4())
    redis_conn.set(_token_key(token), user_id, ex=AUTH_TOKEN_TTL_SECONDS)
    return token


def resolve_user_id(redis_conn: Redis, token: str | None) -> str | None:
    """Returns the user_id for a valid, non-expired token, or None if the
    cookie is missing/unknown/expired — callers turn None into a 401."""
    if not token:
        return None
    raw = redis_conn.get(_token_key(token))
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else raw
