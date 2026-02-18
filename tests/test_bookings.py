# Booking API test suite: happy paths, approval flow, overlap rules, expiry sweeper, and authorization checks.
from __future__ import annotations

from typing import Tuple

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.sweepers import sweep_expired_bookings
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


# Helper: login and return (access_token, user JSON)
def login(client: TestClient, email: str, password: str) -> Tuple[str, dict]:
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
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


def create_booking(client: TestClient, token: str, property_id: int, start_date: str, end_date: str) -> dict:
    """
    Helper: POST /bookings and return a simplified envelope:
      {
        "status_code": number,
        "data": JSON | text
      }

    On 201, JSON shape:
      {
        "booking": { ... },
        "next_action": {"type":"await_approval"} | {"type":"pay","expires_at":"...","client_secret":"..."}
      }
    """
    r = client.post(
        "/api/v1/bookings",
        headers=auth_headers(token),
        json={"property_id": property_id, "start_date": start_date, "end_date": end_date},
    )
    return {"status_code": r.status_code, "data": (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)}


# Auto path: property without approval -> booking goes to pending_payment with an expires_at and pay next_action
def test_booking_auto_path_pending_payment(client: TestClient):
    # Create landlord and property (no approval required)
    landlord_token, landlord = signup(client, "host@example.com", "changeme123", "landlord")
    prop = create_property(client, landlord_token, "Test Place", 12345, requires_approval=False)

    # Create tenant and request booking → pending_payment + expires_at
    tenant_token, tenant = signup(client, "guest@example.com", "changeme123", "tenant")
    start, end = "2025-01-10", "2025-01-12"  # 2 nights
    res = create_booking(client, tenant_token, prop["id"], start, end)
    assert res["status_code"] == 201, res["data"]
    payload = res["data"]
    assert "booking" in payload and "next_action" in payload, payload
    booking = payload["booking"]
    next_action = payload["next_action"]

    assert booking["property_id"] == prop["id"]
    assert booking["guest_id"] == tenant["id"]
    assert booking["status"] == "pending_payment"
    assert booking["currency"] == "USD"
    assert booking["total_cents"] == 12345 * 2
    assert "expires_at" in booking and booking["expires_at"] is not None

    assert next_action["type"] == "pay"
    assert "expires_at" in next_action and next_action["expires_at"] is not None

    # List my bookings (tenant)
    r = client.get("/api/v1/bookings/me", headers=auth_headers(tenant_token))
    assert r.status_code == 200
    items = r.json()
    assert any(b["id"] == booking["id"] for b in items)


# Approval flow: requested -> approve -> pending_payment; separate request can be declined
def test_booking_requires_approval_then_approve_and_decline(client: TestClient):
    landlord_token, landlord = signup(client, "host2@example.com", "changeme123", "landlord")
    prop = create_property(client, landlord_token, "Approval Place", 20000, requires_approval=True)
    tenant_token, tenant = signup(client, "guest2@example.com", "changeme123", "tenant")

    # Request booking → requested + await_approval
    res1 = create_booking(client, tenant_token, prop["id"], "2025-02-10", "2025-02-12")
    assert res1["status_code"] == 201, res1["data"]
    payload1 = res1["data"]
    booking1 = payload1["booking"]
    na1 = payload1["next_action"]
    assert booking1["status"] == "requested"
    assert na1["type"] == "await_approval"

    # Approve as landlord → pending_payment + expires_at
    r = client.post(f"/api/v1/bookings/{booking1['id']}/approve", headers=auth_headers(landlord_token))
    assert r.status_code == 200, r.text
    b_after = r.json()
    assert b_after["status"] == "pending_payment"
    assert b_after["expires_at"] is not None

    # New request then decline
    res2 = create_booking(client, tenant_token, prop["id"], "2025-03-10", "2025-03-12")
    assert res2["status_code"] == 201, res2["data"]
    payload2 = res2["data"]
    booking2 = payload2["booking"]
    assert booking2["status"] == "requested"
    r2 = client.post(f"/api/v1/bookings/{booking2['id']}/decline", headers=auth_headers(landlord_token))
    assert r2.status_code == 200, r2.text
    declined = r2.json()
    assert declined["status"] == "declined"
    assert declined["cancel_reason"] == "declined"


# Overlap rule: only confirmed bookings block new requests; pending_payment does not
def test_overlap_only_confirmed_blocks(client: TestClient):
    landlord_token, _ = signup(client, "host3@example.com", "changeme123", "landlord")
    prop = create_property(client, landlord_token, "Overlap Place", 30000, requires_approval=False)
    tenant_token, _ = signup(client, "guest3@example.com", "changeme123", "tenant")

    # First booking (pending_payment)
    res1 = create_booking(client, tenant_token, prop["id"], "2025-04-10", "2025-04-12")
    assert res1["status_code"] == 201, res1["data"]
    b1 = res1["data"]["booking"]

    # Overlapping booking should be allowed (pending_* does not block)
    res2 = create_booking(client, tenant_token, prop["id"], "2025-04-11", "2025-04-13")
    assert res2["status_code"] == 201, res2["data"]

    # Force-confirm first booking in DB, then overlapping should conflict
    db = SessionLocal()
    try:
        obj = db.get(models.Booking, b1["id"])
        assert obj is not None
        obj.status = "confirmed"
        db.add(obj)
        db.commit()
    finally:
        db.close()

    res3 = create_booking(client, tenant_token, prop["id"], "2025-04-11", "2025-04-13")
    assert res3["status_code"] == 409, res3["data"]


# Expiry sweeper: pending_payment past its hold window becomes cancelled_expired
def test_expiry_sweeper_converts_pending_to_cancelled_expired(client: TestClient):
    landlord_token, _ = signup(client, "host4@example.com", "changeme123", "landlord")
    prop = create_property(client, landlord_token, "Expire Place", 15000, requires_approval=False)
    tenant_token, tenant = signup(client, "guest4@example.com", "changeme123", "tenant")

    # Create pending_payment
    res = create_booking(client, tenant_token, prop["id"], "2025-05-10", "2025-05-12")
    assert res["status_code"] == 201, res["data"]
    booking = res["data"]["booking"]

    # Force expires_at in the past
    db = SessionLocal()
    try:
        obj = db.get(models.Booking, booking["id"])
        assert obj is not None
        obj.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.add(obj)
        db.commit()
    finally:
        db.close()

    # Run sweeper
    n = sweep_expired_bookings()
    assert n >= 1

    # Fetch and assert status
    r = client.get("/api/v1/bookings/me", headers=auth_headers(tenant_token))
    assert r.status_code == 200
    items = r.json()
    found = next(b for b in items if b["id"] == booking["id"])
    assert found["status"] == "cancelled_expired"


# Authorization: only owner landlord can approve/decline; tenant can cancel own booking
def test_authorization_for_approve_decline_and_cancel(client: TestClient):
    # Landlord A owns property; Landlord B tries to approve -> 403
    token_a, user_a = signup(client, "hostA@example.com", "changeme123", "landlord")
    token_b, user_b = signup(client, "hostB@example.com", "changeme123", "landlord")
    prop_a = create_property(client, token_a, "Host A Place", 10000, requires_approval=True)

    tenant_token, tenant = signup(client, "guest5@example.com", "changeme123", "tenant")
    res = create_booking(client, tenant_token, prop_a["id"], "2025-06-10", "2025-06-12")
    assert res["status_code"] == 201, res["data"]
    booking = res["data"]["booking"]
    assert booking["status"] == "requested"

    # Landlord B cannot approve
    r = client.post(f"/api/v1/bookings/{booking['id']}/approve", headers=auth_headers(token_b))
    assert r.status_code == 403

    # Tenant cannot approve
    r2 = client.post(f"/api/v1/bookings/{booking['id']}/approve", headers=auth_headers(tenant_token))
    assert r2.status_code in (401, 403)

    # Tenant can cancel own booking
    r3 = client.delete(f"/api/v1/bookings/{booking['id']}", headers=auth_headers(tenant_token))
    assert r3.status_code == 200
    assert r3.json()["status"] == "cancelled"
