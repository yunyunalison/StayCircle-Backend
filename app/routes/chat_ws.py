# WebSocket chat endpoints and Redis fan-out for per-property chat rooms.
# Provides local in-process broadcast plus optional Redis Pub/Sub for multi-worker environments.
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Dict, Set, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from starlette.websockets import WebSocketState
from sqlalchemy.orm import Session
import threading
from ..redis_client import get_redis, is_redis_enabled

from ..db import SessionLocal
from .. import models
from .auth import decode_token  # reuse JWT verification from REST

router = APIRouter()
logger = logging.getLogger("staycircle.chat")


class TokenBucket:
    """
    Minimal token-bucket rate limiter.

    Parameters:
    - rate: tokens added per second
    - capacity: maximum burst size

    Calling consume(1) returns True if allowed; False if throttled.
    """
    def __init__(self, rate: float, capacity: int) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.ts = time.monotonic()

    def consume(self, amount: float = 1.0) -> bool:
        now = time.monotonic()
        delta = now - self.ts
        self.ts = now
        # Refill tokens
        self.tokens = min(self.capacity, self.tokens + delta * self.rate)
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


class ConnectionManager:
    """
    Track active connections by property and maintain per-connection rate limiters.

    Thread-safety:
    - Uses an asyncio.Lock to guard mutations to internal maps.
    """
    def __init__(self) -> None:
        self.rooms: Dict[int, Set[WebSocket]] = {}
        self.limiters: Dict[WebSocket, TokenBucket] = {}
        self._lock = asyncio.Lock()

    async def connect(self, property_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.rooms.setdefault(property_id, set()).add(websocket)
            self.limiters[websocket] = TokenBucket(rate=1.0, capacity=5)

    async def disconnect(self, property_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            if property_id in self.rooms:
                self.rooms[property_id].discard(websocket)
                if not self.rooms[property_id]:
                    del self.rooms[property_id]
            self.limiters.pop(websocket, None)

    def get_limiter(self, websocket: WebSocket) -> Optional[TokenBucket]:
        return self.limiters.get(websocket)

    async def broadcast(self, property_id: int, message_text: str) -> None:
        # Copy recipients to avoid iterating a mutating set
        recipients = list(self.rooms.get(property_id, set()))
        for ws in recipients:
            try:
                if ws.application_state == WebSocketState.CONNECTED:
                    await ws.send_text(message_text)
            except Exception:
                # Best-effort cleanup
                try:
                    await ws.close()
                except Exception:
                    pass


manager = ConnectionManager()


def start_redis_subscriber(loop: asyncio.AbstractEventLoop) -> None:
    """
    Start a daemon thread that subscribes to 'chat:property:*' and relays messages to local WebSocket clients.

    Behavior:
    - Best-effort fail-open with exponential backoff when Redis is unavailable.
    - Parses property_id from payload or channel name and schedules a broadcast on the given event loop.
    """
    if not is_redis_enabled():
        logger.info("redis.subscriber.disabled")
        return

    def _run() -> None:
        backoff = 0.5
        max_backoff = 5.0
        while True:
            try:
                r = get_redis()
                if r is None:
                    time.sleep(min(backoff, max_backoff))
                    backoff = min(max_backoff, backoff * 2)
                    continue

                pubsub = r.pubsub()
                pubsub.psubscribe("chat:property:*")
                logger.info("redis.subscriber.started", extra={})
                backoff = 0.5  # reset on success
                for message in pubsub.listen():
                    if message is None:
                        continue
                    if message.get("type") != "pmessage":
                        continue
                    data = message.get("data")
                    try:
                        if isinstance(data, bytes):
                            data_str = data.decode("utf-8")
                        else:
                            data_str = str(data)
                        # Broadcast payload as-is; clients de-dup by id if needed
                        # Extract property_id for routing
                        try:
                            payload = json.loads(data_str)
                            prop_id = int(payload.get("property_id"))
                        except Exception:
                            # Fallback parse from channel if payload missing or malformed
                            channel = message.get("channel")
                            if isinstance(channel, bytes):
                                channel = channel.decode("utf-8")
                            prop_id = int(str(channel).split(":")[-1])
                        asyncio.run_coroutine_threadsafe(manager.broadcast(prop_id, data_str), loop)
                    except Exception:
                        # swallow and continue
                        continue
            except Exception:
                # reconnect with backoff
                time.sleep(min(backoff, max_backoff))
                backoff = min(max_backoff, backoff * 2)

    t = threading.Thread(target=_run, name="redis-subscriber", daemon=True)
    t.start()


def _get_token_from_ws(websocket: WebSocket) -> Optional[str]:
    # Prefer Authorization header if present (supports 'Authorization: Bearer <token>')
    auth = websocket.headers.get("authorization") or websocket.headers.get("Authorization")
    if auth:
        parts = auth.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1]
    # Fallback to `?token=` query parameter
    token = websocket.query_params.get("token")
    if token:
        return token
    return None


def _load_user_and_authorize(db: Session, token: str, property_id: int) -> models.User:
    """
    Validate JWT, load the user and property, and enforce authorization rules.

    Rules:
    - tenant: may join any property's chat
    - landlord: must own the property
    """
    # Decode JWT
    payload = decode_token(token)  # raises HTTPException(401) on failure
    sub = payload.get("sub")
    if not sub:
        raise RuntimeError("Invalid token payload (no sub)")

    user = db.get(models.User, int(sub))
    if not user:
        raise RuntimeError("User not found")

    prop = db.get(models.Property, property_id)
    if not prop:
        raise RuntimeError("Property not found")

    # Authorization:
    # - tenant: allowed
    # - landlord: must own the property
    if user.role == "landlord":
        if prop.owner_id != user.id:
            raise PermissionError("Not owner of property")
    elif user.role == "tenant":
        pass
    else:
        raise PermissionError("Forbidden")
    return user


@router.websocket("/chat/property/{property_id}")
async def property_chat(websocket: WebSocket, property_id: int) -> None:
    """
    WebSocket chat for a single property.

    Authentication:
    - JWT required via 'Authorization: Bearer <token>' header or ?token= query parameter

    Authorization:
    - Tenant: allowed
    - Landlord: must own the property

    Message shapes:
    - Client -> Server: {"text": "..."} with 1..1000 characters (whitespace trimmed)
    - Server -> Clients: {"id","property_id","sender_id","text","created_at"}

    Rate limiting:
    - Per-connection 1 msg/s with burst capacity of 5
    """
    # Perform authentication and authorization before accept
    db: Session = SessionLocal()
    user: Optional[models.User] = None
    try:
        token = _get_token_from_ws(websocket)
        if not token:
            await websocket.close(code=1008)  # Policy violation
            return

        try:
            user = _load_user_and_authorize(db, token, property_id)
        except PermissionError:
            await websocket.close(code=1008)
            return
        except RuntimeError:
            # Invalid token payload or property not found
            await websocket.close(code=1008)
            return
        except Exception:
            await websocket.close(code=1008)
            return

        await manager.connect(property_id, websocket)
        logger.info(
            "chat.ws.connected",
            extra={"property_id": property_id, "user_id": user.id, "role": user.role},
        )

        # Main receive loop
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except Exception:
                # Unexpected error reading frame -> close
                break

            # Expect JSON with {"text": "..."}
            try:
                payload = json.loads(raw)
            except Exception:
                await _send_ws_error(websocket, "invalid_json", "Payload must be JSON")
                continue

            text = payload.get("text")
            if not isinstance(text, str):
                await _send_ws_error(websocket, "invalid_payload", "Missing 'text' string")
                continue

            text = text.strip()
            if not (1 <= len(text) <= 1000):
                await _send_ws_error(websocket, "invalid_text", "Text length must be 1..1000")
                continue

            # Rate-limit per connection
            limiter = manager.get_limiter(websocket)
            if limiter is None or not limiter.consume(1.0):
                await _send_ws_error(websocket, "rate_limited", "Too many messages")
                continue

            # Persist and broadcast
            try:
                msg = models.Message(property_id=property_id, sender_id=user.id, text=text)
                db.add(msg)
                db.commit()
                db.refresh(msg)
            except Exception:
                db.rollback()
                await _send_ws_error(websocket, "server_error", "Failed to persist message")
                continue

            out = {
                "id": msg.id,
                "property_id": msg.property_id,
                "sender_id": msg.sender_id,
                "text": msg.text,
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
            }
            out_text = json.dumps(out)

            await manager.broadcast(property_id, out_text)
            # Optional Redis publish for cross-process fan-out
            try:
                r = get_redis()
                if r is not None:
                    channel = f"chat:property:{property_id}"
                    r.publish(channel, out_text)
            except Exception:
                logger.warning("redis.publish.failed", extra={"property_id": property_id})

            logger.info(
                "chat.ws.message",
                extra={
                    "property_id": property_id,
                    "user_id": user.id,
                    "role": user.role,
                    "message_id": msg.id,
                },
            )

    finally:
        try:
            if user is not None:
                await manager.disconnect(property_id, websocket)
                logger.info(
                    "chat.ws.disconnected",
                    extra={"property_id": property_id, "user_id": user.id, "role": user.role},
                )
        except Exception:
            pass
        db.close()


async def _send_ws_error(ws: WebSocket, code: str, message: str) -> None:
    """
    Send a structured error frame to the client; close the socket on failure.
    """
    try:
        await ws.send_text(json.dumps({"type": "error", "code": code, "message": message}))
    except Exception:
        try:
            await ws.close(code=1008)
        except Exception:
            pass
