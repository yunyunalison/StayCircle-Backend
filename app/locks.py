# Distributed locking helpers backed by Redis to gate critical sections across processes.
# Designed to fail open so the application remains available if Redis is down.
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator
from uuid import uuid4

from .redis_client import get_redis

# Namespaced logger for lock acquisition/release diagnostics
logger = logging.getLogger("staycircle.locks")


@contextmanager
def redis_try_lock(key: str, ttl_ms: int = 5000) -> Iterator[bool]:
    """
    Best-effort distributed lock implemented with Redis SET NX PX.

    Behavior:
    - True when the lock is acquired, or when Redis is unavailable (fail-open).
    - False when another process holds the lock.
    - Unlock uses a token-checked Lua script to avoid releasing a lock we don't own.

    Notes:
    - Keep TTLs small; this is a coarse per-resource guard (e.g., per property).
    - Use as a context manager:

        with redis_try_lock(f"lock:booking:property:{pid}", ttl_ms=5000) as locked:
            if not locked:
                raise HTTPException(429, "please retry")
            # critical section
    """
    r = get_redis()
    if r is None:
        # Fail-open if Redis is disabled/unavailable
        yield True
        return

    token = uuid4().hex
    acquired = False
    try:
        # SET key token NX PX ttl_ms returns True on success, falsy/None otherwise
        acquired = bool(r.set(key, token, nx=True, px=ttl_ms))
        yield acquired
    except Exception as exc:
        # Fail open on unexpected Redis errors; proceed without the lock
        logger.warning("redis_try_lock error (key=%s): %s", key, exc)
        yield True
    finally:
        if acquired:
            # Release only if we still own the lock (token matches current value)
            try:
                r.eval(
                    """
                    if redis.call('get', KEYS[1]) == ARGV[1] then
                        return redis.call('del', KEYS[1])
                    else
                        return 0
                    end
                    """,
                    1,
                    key,
                    token,
                )
            except Exception as exc:
                # Do not raise; the lock will expire by TTL
                logger.debug("redis_try_lock release error (key=%s): %s", key, exc)
