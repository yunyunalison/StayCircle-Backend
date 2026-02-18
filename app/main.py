# Application entrypoint: configures middleware, startup routines, and API routers.
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os
import threading
import time

from .db import Base, engine
from .routes.properties import router as properties_router
from .routes.auth import router as auth_router
from .routes.bookings import router as bookings_router
from .routes.messages import router as messages_router
from .routes.chat_ws import router as chat_ws_router, start_redis_subscriber
from .payments import router as payments_router
from .sweepers import sweep_expired_bookings


def _start_expiry_sweeper(interval_seconds: int = 60) -> None:
    """
    Launch a daemon thread that periodically releases bookings stuck in 'pending_payment'.

    Behavior:
    - Call sweep_expired_bookings()
    - Sleep for `interval_seconds`
    Any exception is swallowed to keep the worker alive; it will try again on the next interval.
    """
    def _loop() -> None:
        while True:
            try:
                sweep_expired_bookings()
            except Exception:
                # Keep the worker alive on transient errors; retry on the next interval.
                pass
            time.sleep(interval_seconds)

    t = threading.Thread(target=_loop, name="booking-expiry-sweeper", daemon=True)
    t.start()


# Parse CORS origins from a comma-separated env var.
# Note: '*' cannot be used with allow_credentials=True; we fall back to explicit localhost origins for dev.
def _parse_cors_origins(env_value: str | None) -> list[str]:
    default_dev_origins = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    if not env_value:
        return default_dev_origins
    
    origins = [o.strip() for o in env_value.split(",") if o.strip()]
    # Map '*' to explicit localhost origins so credentialed requests remain allowed
    if "*" in origins:
        return default_dev_origins

    return origins


app = FastAPI(title="StayCircle API", version="0.1.0")
allow_list = _parse_cors_origins(os.getenv("CORS_ORIGINS"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    # For local SQLite, auto-create tables; production DBs rely on Alembic migrations.
    if os.getenv("DATABASE_URL", "sqlite:///./data.db").startswith("sqlite"):
        Base.metadata.create_all(bind=engine)
    # Kick off the background sweeper that releases expired holds (every 60s)
    _start_expiry_sweeper(interval_seconds=60)
    # Start the Redis Pub/Sub subscriber used to fan out chat messages across processes
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        start_redis_subscriber(loop)
    except Exception:
        # Fail open: if Redis is absent, the API still starts; chat fan-out simply won't run
        pass


# Simple liveness endpoint for container orchestrators and uptime checks
@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


# Mount application routers (authentication, payments, domain APIs, and WebSocket chat)
app.include_router(auth_router, prefix="", tags=["auth"])
app.include_router(payments_router, prefix="", tags=["payments"])
app.include_router(properties_router, prefix="/api/v1", tags=["properties"])
app.include_router(bookings_router, prefix="/api/v1", tags=["bookings"])
app.include_router(messages_router, prefix="/api/v1", tags=["messages"])
app.include_router(chat_ws_router, prefix="/ws", tags=["chat"])
