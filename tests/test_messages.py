# Messages HTTP API test suite: ordering, pagination, and authorization checks.
from __future__ import annotations

from typing import Tuple, List

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


def insert_messages(property_id: int, sender_id: int, texts: List[str]) -> List[int]:
    """
    Insert messages for a property from a single sender.

    Returns:
    - List of created message IDs in insertion order.
    """
    db = SessionLocal()
    ids: List[int] = []
    try:
        for t in texts:
            m = models.Message(property_id=property_id, sender_id=sender_id, text=t)
            db.add(m)
            db.flush()  # get id without needing refresh after commit
            ids.append(m.id)
        db.commit()
    finally:
        db.close()
    return ids


# History: results are ordered asc by created_at,id and since_id paginates strictly forward
def test_messages_history_ordering_and_pagination(client: TestClient):
    # Setup: landlord owner, property, tenant
    landlord_token, landlord = signup(client, "owner@example.com", "changeme123", "landlord")
    prop = create_property(client, landlord_token, "Chat Place", 10000, requires_approval=False)
    tenant_token, tenant = signup(client, "guest@example.com", "changeme123", "tenant")

    # Seed messages (tenant → 3 messages)
    ids = insert_messages(prop["id"], tenant["id"], ["hello 1", "hello 2", "hello 3"])
    assert len(ids) == 3 and ids == sorted(ids)

    # Fetch first page (limit=2) as tenant
    r1 = client.get(f"/api/v1/messages?property_id={prop['id']}&limit=2", headers=auth_headers(tenant_token))
    assert r1.status_code == 200, r1.text
    page1 = r1.json()
    assert [m["id"] for m in page1] == ids[:2], page1
    assert all(page1[i]["id"] < page1[i + 1]["id"] for i in range(len(page1) - 1))

    # Fetch next page via since_id
    last_id = page1[-1]["id"]
    r2 = client.get(
        f"/api/v1/messages?property_id={prop['id']}&since_id={last_id}&limit=50",
        headers=auth_headers(tenant_token),
    )
    assert r2.status_code == 200, r2.text
    page2 = r2.json()
    assert [m["id"] for m in page2] == ids[2:], page2


# Authorization: only the owner landlord can read history for their property
def test_messages_authz_landlord_owner_allowed_and_non_owner_forbidden(client: TestClient):
    # Landlord A owns property; Landlord B does not
    token_a, user_a = signup(client, "hostA@example.com", "changeme123", "landlord")
    token_b, user_b = signup(client, "hostB@example.com", "changeme123", "landlord")
    prop_a = create_property(client, token_a, "Host A Place", 12000, requires_approval=False)

    # Tenant and one message
    tenant_token, tenant = signup(client, "guest2@example.com", "changeme123", "tenant")
    _ = insert_messages(prop_a["id"], tenant["id"], ["hi host"])

    # Owner landlord can read
    r_ok = client.get(f"/api/v1/messages?property_id={prop_a['id']}", headers=auth_headers(token_a))
    assert r_ok.status_code == 200, r_ok.text
    assert isinstance(r_ok.json(), list)

    # Non-owner landlord forbidden
    r_forbidden = client.get(f"/api/v1/messages?property_id={prop_a['id']}", headers=auth_headers(token_b))
    assert r_forbidden.status_code == 403, r_forbidden.text


# Authorization: tenant allowed, anonymous denied (401)
def test_messages_authz_tenant_allowed_and_anonymous_denied(client: TestClient):
    # Setup property with owner and tenant
    landlord_token, landlord = signup(client, "hostC@example.com", "changeme123", "landlord")
    prop = create_property(client, landlord_token, "Public Inquiry Place", 9000, requires_approval=False)
    tenant_token, tenant = signup(client, "guest3@example.com", "changeme123", "tenant")
    _ = insert_messages(prop["id"], tenant["id"], ["hello there"])

    # Tenant can read
    r_tenant = client.get(f"/api/v1/messages?property_id={prop['id']}", headers=auth_headers(tenant_token))
    assert r_tenant.status_code == 200, r_tenant.text

    # Anonymous denied (no Authorization header)
    r_anon = client.get(f"/api/v1/messages?property_id={prop['id']}")
    assert r_anon.status_code == 401


# Validation: limit bounds enforced by FastAPI (0 and >100 rejected)
def test_messages_limit_bounds_validation(client: TestClient):
    landlord_token, _ = signup(client, "hostD@example.com", "changeme123", "landlord")
    prop = create_property(client, landlord_token, "Bounds Place", 5000, requires_approval=False)
    tenant_token, tenant = signup(client, "guest4@example.com", "changeme123", "tenant")
    _ = insert_messages(prop["id"], tenant["id"], ["m1"])

    # limit too low -> 422
    r_low = client.get(f"/api/v1/messages?property_id={prop['id']}&limit=0", headers=auth_headers(tenant_token))
    assert r_low.status_code == 422, r_low.text

    # limit too high -> 422
    r_high = client.get(f"/api/v1/messages?property_id={prop['id']}&limit=101", headers=auth_headers(tenant_token))
    assert r_high.status_code == 422, r_high.text
