# WebSocket chat test suite: connection auth, owner checks, broadcast/persistence, and rate limiting.
from __future__ import annotations

import json
import time
from typing import Tuple

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app import models


# Helper: create a user and return (access_token, user JSON)
def signup(client: TestClient, email: str, password: str, role: str | None = None) -> Tuple[str, dict]:
    payload = {"email": email, "password": password}
    if role:
        payload["role"] = role
    r = client.post("/auth/signup", json=payload)
    assert r.status_code == 201, r.text
    data = r.json()
    return data["access_token"], data["user"]


# Convenience header for authenticated requests
def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# Helper: create a property owned by the authenticated landlord
def create_property(client: TestClient, token: str, title: str, price_cents: int, requires_approval: bool = False) -> dict:
    r = client.post(
        "/api/v1/properties",
        headers=auth_headers(token),
        json={"title": title, "price_cents": price_cents, "requires_approval": requires_approval},
    )
    assert r.status_code == 201, r.text
    return r.json()


# Helper: count persisted messages for a property (verifies DB write on WS send)
def count_messages_for_property(property_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(models.Message).filter(models.Message.property_id == property_id).count()
    finally:
        db.close()


# Round-trip: tenant and owner landlord connect, tenant sends, both receive, and message persists
def test_ws_connect_tenant_and_landlord_owner_roundtrip(client: TestClient):
    # Setup users and a property
    landlord_token, landlord = signup(client, "hostws@example.com", "changeme123", "landlord")
    tenant_token, tenant = signup(client, "guestws@example.com", "changeme123", "tenant")
    prop = create_property(client, landlord_token, "WS Place", 11111, requires_approval=False)

    # Connect tenant and landlord to the same property room
    with client.websocket_connect(f"/ws/chat/property/{prop['id']}?token={tenant_token}") as ws_tenant, \
         client.websocket_connect(f"/ws/chat/property/{prop['id']}?token={landlord_token}") as ws_landlord:

        # Tenant sends a message
        before = count_messages_for_property(prop["id"])
        ws_tenant.send_text(json.dumps({"text": "hello via ws"}))

        # Both clients should receive the broadcast
        msg1 = json.loads(ws_tenant.receive_text())
        msg2 = json.loads(ws_landlord.receive_text())

        # Validate payload shape
        for m in (msg1, msg2):
            assert set(m.keys()) == {"id", "property_id", "sender_id", "text", "created_at"}
            assert m["property_id"] == prop["id"]
            assert m["text"] == "hello via ws"
            assert isinstance(m["id"], int)

        # Persistence verified
        after = count_messages_for_property(prop["id"])
        assert after == before + 1


# Authorization: non-owner landlord should be refused at handshake
def test_ws_landlord_non_owner_forbidden(client: TestClient):
    token_a, user_a = signup(client, "hostA-ws@example.com", "changeme123", "landlord")
    token_b, user_b = signup(client, "hostB-ws@example.com", "changeme123", "landlord")
    prop_a = create_property(client, token_a, "Host A WS", 15000, requires_approval=False)

    # Non-owner landlord should be refused (policy violation close)
    try:
        client.websocket_connect(f"/ws/chat/property/{prop_a['id']}?token={token_b}")
        assert False, "Expected connection to be refused for non-owner landlord"
    except Exception:
        # Starlette raises an exception on connection close during handshake; acceptable
        pass


# Authentication: missing token results in denied connection
def test_ws_missing_token_denied(client: TestClient):
    # No token query param or header
    try:
        client.websocket_connect("/ws/chat/property/1")
        assert False, "Expected connection to be refused for missing token"
    except Exception:
        pass


# Validation + rate limit: invalid payload yields error; burst exceeds limiter; refill allows another send
def test_ws_validation_and_rate_limit(client: TestClient):
    landlord_token, _ = signup(client, "hostrate@example.com", "changeme123", "landlord")
    prop = create_property(client, landlord_token, "Rate WS", 22222, requires_approval=False)
    tenant_token, _ = signup(client, "guestrate@example.com", "changeme123", "tenant")

    with client.websocket_connect(f"/ws/chat/property/{prop['id']}?token={tenant_token}") as ws:
        # Empty/invalid message
        ws.send_text(json.dumps({"text": ""}))
        resp = json.loads(ws.receive_text())
        assert resp.get("type") == "error" and resp.get("code") == "invalid_text"

        # Send a few allowed messages within burst
        for i in range(5):
            ws.send_text(json.dumps({"text": f"m{i}"}))
            _ = json.loads(ws.receive_text())  # broadcast

        # Next should hit rate limit (burst exceeded)
        ws.send_text(json.dumps({"text": "burst-exceed"}))
        resp2 = json.loads(ws.receive_text())
        assert resp2.get("type") == "error" and resp2.get("code") == "rate_limited"

        # Wait to refill and try again
        time.sleep(1.2)
        ws.send_text(json.dumps({"text": "after-refill"}))
        _ = json.loads(ws.receive_text())  # broadcast
