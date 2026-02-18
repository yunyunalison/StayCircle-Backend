# Simple Redis-backed fixed-window rate limiter.
# - Per-IP counters (no user coupling) to keep the dependency standalone.
# - Keys: rl:v1:ip:{ip}:{scope} with a TTL-based fixed window.
# - Fail-open if Redis is unavailable, so the API remains usable in dev or outages.
import os
import logging
from typing import Callable, Literal, Optional

from fastapi import Request, HTTPException, status

from .redis_client import get_redis, is_redis_enabled

# Namespaced logger for rate limiting diagnostics
logger = logging.getLogger("staycircle.rate_limit")

# Supported scopes with independent per-window limits (see _limit_for_scope)
Scope = Literal["login", "signup", "write"]


def _to_int(val: Optional[str], default: int) -> int:
    try:
        return int(val) if val is not None else default
    except Exception:
        return default


# Window length in seconds; configured via RATE_LIMIT_WINDOW_SECONDS (default 60)
def _window_seconds() -> int:
    return _to_int(os.getenv("RATE_LIMIT_WINDOW_SECONDS"), 60)


# Per-scope cap per window; tune via:
# - RATE_LIMIT_LOGIN_PER_WINDOW (default 10)
# - RATE_LIMIT_SIGNUP_PER_WINDOW (default 5)
# - RATE_LIMIT_WRITE_PER_WINDOW  (default 30)
def _limit_for_scope(scope: Scope) -> int:
    if scope == "login":
        return _to_int(os.getenv("RATE_LIMIT_LOGIN_PER_WINDOW"), 10)
    if scope == "signup":
        return _to_int(os.getenv("RATE_LIMIT_SIGNUP_PER_WINDOW"), 5)
    # default for writes
    return _to_int(os.getenv("RATE_LIMIT_WRITE_PER_WINDOW"), 30)


def _client_ip(request: Request) -> str:
    # Use the connection's remote address.
    # Does not parse X-Forwarded-For; behind a proxy, only trust forwarded headers when properly configured.
    try:
        if request.client and request.client.host:
            return request.client.host
    except Exception:
        pass
    return "unknown"


def rate_limit(scope: Scope) -> Callable[[Request], None]:
    """
    Fixed-window rate limiting using Redis counters.

    Scope:
    - Per-IP only (no user coupling) to avoid auth dependencies.

    Keys:
    - rl:v1:ip:{ip}:{scope}

    Window and limits:
    - Window length: RATE_LIMIT_WINDOW_SECONDS (default 60s)
    - Per-scope caps per window:
        * login:  RATE_LIMIT_LOGIN_PER_WINDOW (default 10)
        * signup: RATE_LIMIT_SIGNUP_PER_WINDOW (default 5)
        * write:  RATE_LIMIT_WRITE_PER_WINDOW  (default 30)

    Behavior:
    - On first hit in a window, the TTL is initialized; subsequent hits share the same expiry.
    - If Redis is disabled or unavailable, the limiter fails open to preserve availability.
    """
    window = _window_seconds()
    limit = _limit_for_scope(scope)

    def _dependency(request: Request) -> None:
        if not is_redis_enabled():
            return

        r = get_redis()
        if r is None:
            # Fail-open: Redis disabled or unreachable
            return

        ip = _client_ip(request)
        key = f"rl:v1:ip:{ip}:{scope}"
        try:
            current = r.incr(key, amount=1)
            if current == 1:
                # Initialize TTL on first increment in this window
                r.expire(key, window)
            if current > limit:
                ttl = r.ttl(key)
                retry_after = ttl if isinstance(ttl, int) and ttl > 0 else window
                detail = {
                    "error": "rate_limited",
                    "scope": scope,
                    "ip": ip,
                    "limit": limit,
                    "window_seconds": window,
                    "retry_after": retry_after,
                }
                raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)
        except HTTPException:
            raise
        except Exception as exc:
            # Fail open on Redis errors to avoid blocking requests
            logger.warning("Rate limit fail-open (scope=%s, ip=%s): %s", scope, ip, exc)
            return

    return _dependency
