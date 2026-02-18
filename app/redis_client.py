# Redis client helper: opt-in, fail-open access to a shared Redis connection.
# Controlled by REDIS_ENABLED and REDIS_URL to keep other modules decoupled from Redis availability.
import logging
import os
from typing import Optional

# Module-scoped logger for connection/health messages
_logger = logging.getLogger("staycircle.redis")


# Basic truthy parser for env flags (1, true, yes, on)
def _truthy(val: Optional[str]) -> bool:
    if val is None:
        return False
    return val.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


# Feature flag: enable Redis by setting REDIS_ENABLED to a truthy value
def is_redis_enabled() -> bool:
    return _truthy(os.getenv("REDIS_ENABLED", "false"))


# Cached client instance (if connected) and a one-shot initialization guard.
# Once initialization is attempted (_initialized=True) and fails, we remain fail-open.
_client = None
_initialized = False


def get_redis():
    """
    Return a Redis client if enabled and reachable; otherwise return None.

    Behavior:
    - Lazy initialization on first call
    - Fail-open on errors (do not raise), so downstream features can degrade gracefully
    - After a failed attempt in this process, subsequent calls also return None
    """
    global _client, _initialized
    if not is_redis_enabled():
        return None
    if _client is not None:
        return _client
    if _initialized and _client is None:
        # Previously attempted and failed; stay fail-open for this process lifetime
        return None

    # Connection URL; defaults to local Redis database 0
    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    try:
        # Import here to avoid import-time failures before dependencies are installed
        import redis  # type: ignore

        _client = redis.Redis.from_url(
            url,
            socket_timeout=0.25,
            socket_connect_timeout=0.25,
            retry_on_timeout=False,
            health_check_interval=0,
        )
        # Ping to verify connectivity and credentials
        _client.ping()
        _initialized = True
        _logger.info("Connected to Redis at %s", url)
        return _client
    except Exception as exc:
        _logger.warning("Redis unavailable (fail-open): %s", exc)
        _client = None
        _initialized = True
        return None
