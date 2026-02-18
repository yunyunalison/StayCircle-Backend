# Payments module: Stripe integration with test-friendly fallbacks.
# Exposes endpoints to create PaymentIntents, fetch client secrets, finalize bookings, and handle webhooks.
# When Stripe keys are absent, operates in deterministic offline mode for local/dev and CI.
from __future__ import annotations

import os
from typing import Tuple, Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from .db import get_db
from . import models, schemas
from .routes.auth import require_tenant

# Stripe SDK is optional; tests/CI may omit keys to stay fully offline.
try:
    import stripe  # type: ignore
except Exception:  # pragma: no cover
    stripe = None  # type: ignore

# Router namespace for payment-related HTTP endpoints
router = APIRouter()

# Environment configuration (blank values disable Stripe features in dev/tests)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()


def stripe_enabled() -> bool:
    """
    True only when the Stripe SDK is importable and STRIPE_SECRET_KEY is set.

    When False, endpoints use deterministic, network-free behavior for tests/dev.
    """
    return bool(stripe and STRIPE_SECRET_KEY)


def _init_stripe() -> None:
    if not stripe_enabled():
        raise RuntimeError("Stripe not enabled (SDK missing or STRIPE_SECRET_KEY not set)")
    stripe.api_key = STRIPE_SECRET_KEY


def create_payment_intent(
    amount_cents: int,
    currency: str,
    booking_id: int,
    property_id: int,
    idempotency_key: str,
) -> Tuple[str, str]:
    """
    Create a PaymentIntent and return (payment_intent_id, client_secret).

    - Production: goes through Stripe (test keys) with automatic_payment_methods for PaymentElement.
    - Dev/CI without Stripe: returns deterministic fake values to keep flows testable/offline.
    """
    if not stripe_enabled():
        # Local/test fallback: generate synthetic IDs and secrets (no network calls)
        fake_pi_id = f"pi_test_{booking_id}"
        fake_cs = f"test_client_secret_{booking_id}"
        return fake_pi_id, fake_cs

    _init_stripe()
    # Use automatic_payment_methods to keep UI simple with PaymentElement
    pi = stripe.PaymentIntent.create(
        amount=int(amount_cents),
        currency=currency.lower(),
        metadata={"booking_id": str(booking_id), "property_id": str(property_id)},
        automatic_payment_methods={"enabled": True},
        idempotency_key=idempotency_key,
    )
    # Idempotency: the Python SDK accepts idempotency_key via request options.
    # Clients must reuse the same key when retrying the same logical request.
    # Here we derive a stable key from booking/version to make retries safe.

    # Retrieve client secret
    client_secret: Optional[str] = getattr(pi, "client_secret", None)
    if not client_secret:
        # Retrieve to ensure we have it (some Stripe flows do not return it on all calls)
        pi = stripe.PaymentIntent.retrieve(pi.id)
        client_secret = getattr(pi, "client_secret", None)
    if not client_secret:
        raise RuntimeError("Stripe PaymentIntent missing client_secret")
    return pi.id, client_secret


def retrieve_client_secret(payment_intent_id: str) -> str:
    """
    Return the client_secret for an existing PaymentIntent.

    Offline mode: when Stripe is disabled, synthesize a deterministic secret so the UI can render.
    """
    if not stripe_enabled():
        # Synthesize a deterministic secret for UI rendering in tests
        return f"test_client_secret_{payment_intent_id}"

    _init_stripe()
    pi = stripe.PaymentIntent.retrieve(payment_intent_id)
    client_secret: Optional[str] = getattr(pi, "client_secret", None)
    if not client_secret:
        raise RuntimeError("Stripe PaymentIntent missing client_secret")
    return client_secret

@router.get("/api/v1/bookings/{booking_id}/payment_info", response_model=schemas.PaymentInfoResponse)
def get_payment_info(
    booking_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_tenant),
) -> schemas.PaymentInfoResponse:
    """
    Ensure a PaymentIntent exists for a 'pending_payment' booking and return:
    - client_secret for the frontend PaymentElement
    - expires_at (hold deadline) for rendering timers

    Authorization: only the booking's tenant may access.
    """
    booking = db.get(models.Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if booking.guest_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    if booking.status != "pending_payment":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Booking is not pending payment")

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    expires_at = booking.expires_at
    if expires_at is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment hold expired")
    # Normalize to timezone-aware (assume UTC) to avoid naive vs aware comparison issues
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment hold expired")

    client_secret: Optional[str] = None
    if not booking.payment_intent_id:
        idem_key = f"booking:{booking.id}:v{booking.version or 1}"
        pi_id, client_secret = create_payment_intent(
            amount_cents=booking.total_cents,
            currency=booking.currency,
            booking_id=booking.id,
            property_id=booking.property_id,
            idempotency_key=idem_key,
        )
        booking.payment_intent_id = pi_id
        db.add(booking)
        db.commit()
    else:
        # If Stripe is enabled, inspect the existing PaymentIntent status:
        # - canceled: create a fresh intent and update the booking/version
        # - succeeded: finalize booking (webhook mirror) and short-circuit the pay flow
        if stripe_enabled():
            try:
                pi = stripe.PaymentIntent.retrieve(booking.payment_intent_id)
                pi_status = getattr(pi, "status", None)
            except Exception:
                pi = None
                pi_status = None

            if pi_status == "canceled":
                # Create a new intent for a canceled/invalidated PI
                idem_key = f"booking:{booking.id}:v{(booking.version or 1) + 1}"
                new_pi_id, client_secret = create_payment_intent(
                    amount_cents=booking.total_cents,
                    currency=booking.currency,
                    booking_id=booking.id,
                    property_id=booking.property_id,
                    idempotency_key=idem_key,
                )
                booking.payment_intent_id = new_pi_id
                booking.version = (booking.version or 1) + 1
                db.add(booking)
                db.commit()
            elif pi_status == "succeeded":
                # Treat as paid: finalize booking just like webhook would (idempotent)
                # Expiry guard
                if expires_at <= now:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment hold expired")
                # Defensive overlap vs confirmed
                if _has_confirmed_overlap(db, booking.property_id, booking.start_date, booking.end_date):
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Overlap conflict")
                current_version = booking.version or 1
                rows = (
                    db.query(models.Booking)
                    .filter(
                        models.Booking.id == booking.id,
                        models.Booking.version == current_version,
                        models.Booking.status == "pending_payment",
                    )
                    .update(
                        {
                            models.Booking.status: "confirmed",
                            models.Booking.version: current_version + 1,
                        },
                        synchronize_session=False,
                    )
                )
                if rows:
                    db.commit()
                else:
                    db.rollback()
                # Return a user-friendly error; the UI should refresh to reflect 'confirmed'
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Booking already paid")

    if client_secret is None:
        client_secret = retrieve_client_secret(booking.payment_intent_id)  # type: ignore[arg-type]
    return schemas.PaymentInfoResponse(
        booking_id=booking.id,
        client_secret=client_secret,
        expires_at=expires_at,  # type: ignore[arg-type]
    )


@router.post("/api/v1/bookings/{booking_id}/finalize_payment")
def finalize_payment(
    booking_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_tenant),
):
    """
    Finalize a booking after client-side confirmation when webhooks are unavailable.

    Idempotency:
    - Already confirmed: return the confirmed booking
    - Still processing: return a 'processing' status so the client can poll
    """
    # Lookup and basic auth
    booking: Optional[models.Booking] = db.get(models.Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if booking.guest_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")

    # If already confirmed, short-circuit
    if booking.status == "confirmed":
        return schemas.BookingRead.model_validate(booking)

    if booking.status != "pending_payment":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Booking is not pending payment")

    # Hold window must still be valid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    exp = booking.expires_at
    if exp is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment hold expired")
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp <= now:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment hold expired")

    # Stripe must be enabled to check PaymentIntent status
    if not stripe_enabled():
        return {"status": "stripe_disabled"}

    _init_stripe()
    if not booking.payment_intent_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing payment_intent")

    try:
        pi = stripe.PaymentIntent.retrieve(booking.payment_intent_id)
        pi_status: Optional[str] = getattr(pi, "status", None)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unable to retrieve PaymentIntent: {exc}")

    if pi_status == "succeeded":
        # Defensive overlap vs confirmed (should not happen, but guard)
        if _has_confirmed_overlap(db, booking.property_id, booking.start_date, booking.end_date):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Overlap conflict")

        # Idempotent finalize (mirror webhook)
        current_version = booking.version or 1
        rows = (
            db.query(models.Booking)
            .filter(
                models.Booking.id == booking.id,
                models.Booking.version == current_version,
                models.Booking.status == "pending_payment",
            )
            .update(
                {
                    models.Booking.status: "confirmed",
                    models.Booking.version: current_version + 1,
                },
                synchronize_session=False,
            )
        )
        if rows == 0:
            db.rollback()
            latest = db.query(models.Booking).get(booking.id)
            if latest and latest.status == "confirmed":
                return schemas.BookingRead.model_validate(latest)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Version conflict")
        db.commit()
        db.refresh(booking)
        return schemas.BookingRead.model_validate(booking)

    if pi_status in ("processing", "requires_action", "requires_payment_method", "requires_confirmation"):
        # Let client retry/poll
        return {"status": "processing"}

    if pi_status == "canceled":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment cancelled")

    # Unknown/unexpected status
    return {"status": pi_status or "unknown"}


def _has_confirmed_overlap(db: Session, property_id: int, start_date, end_date) -> bool:
    """
    Return True if any confirmed booking overlaps the given [start_date, end_date).

    Overlap logic:
    NOT (existing.end_date <= start_date OR existing.start_date >= end_date)
    """
    exists = (
        db.query(models.Booking.id)
        .filter(
            models.Booking.property_id == property_id,
            models.Booking.status == "confirmed",
            ~(
                (models.Booking.end_date <= start_date)
                | (models.Booking.start_date >= end_date)
            ),
        )
        .first()
    )
    return exists is not None


@router.post("/payments/webhook")
async def stripe_webhook(
    request: Request,
    db: Session = Depends(get_db),
    stripe_signature: str = Header(None, alias="Stripe-Signature"),
) -> dict:
    """
    Verify Stripe signature and handle payment_intent.succeeded by finalizing a booking idempotently.

    Always return 200 for successful processing or safe no-ops; return 4xx only for invalid payload/signature.
    """
    # If Stripe is disabled, accept as a no-op to keep local/dev flows simple
    if not stripe_enabled():
        return {"status": "stripe_disabled"}

    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook secret not configured")

    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload=payload.decode("utf-8"),
            sig_header=stripe_signature,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid webhook: {exc}")

    event_type: str = getattr(event, "type", "") or ""
    obj: Any = getattr(event, "data", {}).get("object") if hasattr(event, "data") else None

    if event_type == "payment_intent.succeeded" and obj:
        payment_intent_id = getattr(obj, "id", None) or (obj.get("id") if isinstance(obj, dict) else None)
        if not payment_intent_id:
            # Malformed event
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing payment_intent id")

        # Lookup booking by payment_intent_id
        booking: Optional[models.Booking] = (
            db.query(models.Booking).filter(models.Booking.payment_intent_id == payment_intent_id).first()
        )
        if not booking:
            # Unknown intent; accept to avoid a retry storm (could log for investigation)
            return {"status": "unknown_intent"}

        # Preconditions
        if booking.status == "confirmed":
            return {"status": "already_confirmed"}  # idempotent

        if booking.status != "pending_payment":
            return {"status": "invalid_status"}  # ignore

        # Expiry guard
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        exp = booking.expires_at
        if exp is None:
            return {"status": "expired"}  # ignore late
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            return {"status": "expired"}  # ignore late

        # Defensive overlap check against confirmed bookings
        if _has_confirmed_overlap(db, booking.property_id, booking.start_date, booking.end_date):
            return {"status": "overlap_conflict"}  # ignore/alert; do not confirm

        # Finalize using optimistic concurrency control
        current_version = booking.version or 1
        rows = (
            db.query(models.Booking)
            .filter(
                models.Booking.id == booking.id,
                models.Booking.version == current_version,
                models.Booking.status == "pending_payment",
            )
            .update(
                {
                    models.Booking.status: "confirmed",
                    models.Booking.version: current_version + 1,
                },
                synchronize_session=False,
            )
        )
        if rows == 0:
            # Re-read to determine the latest state
            db.rollback()  # rollback pending transaction to clear write intents
            latest = db.query(models.Booking).get(booking.id)
            if latest and latest.status == "confirmed":
                return {"status": "already_confirmed"}
            # Could retry limited times in a real system; here just surface a conflict-ish outcome
            return {"status": "version_conflict"}
        db.commit()
        return {"status": "confirmed"}

    # Optionally log failures; do not error
    if event_type == "payment_intent.payment_failed":
        return {"status": "payment_failed_observed"}

    # Other events ignored
    return {"status": "ignored"}
