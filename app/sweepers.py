# Background sweepers for periodic maintenance tasks (e.g., expiring unpaid holds).
# These utilities are invoked from startup threads or scheduler jobs.
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .db import SessionLocal
from . import models


def sweep_expired_bookings(db: Optional[Session] = None) -> int:
    """
    Mark expired 'pending_payment' bookings as 'cancelled_expired'.

    Semantics:
    - Only processes rows with status == 'pending_payment' and expires_at < now (UTC).
    - Idempotent across repeated runs.
    - Accepts an optional Session; otherwise creates and cleans up its own.

    Returns:
    - Number of rows considered/updated.
    """
    # Track whether this call created its own DB session (so we can close it)
    created_session = False
    if db is None:
        db = SessionLocal()
        created_session = True

    try:
        now = datetime.now(timezone.utc)
        # Find all bookings that are still pending payment, but whose holds have expired
        items = (
            db.query(models.Booking)
            .filter(
                models.Booking.status == "pending_payment",
                models.Booking.expires_at != None,  # noqa: E711
                models.Booking.expires_at < now,
            )
            .all()
        )
        for obj in items:
            # Idempotent + timezone-aware guard
            # Treat naive datetimes (e.g., SQLite) as UTC before comparing.
            exp = obj.expires_at
            if exp is not None and getattr(exp, "tzinfo", None) is None:
                # Some backends (e.g., SQLite) may return naive datetimes; treat stored values as UTC.
                exp = exp.replace(tzinfo=timezone.utc)
            if obj.status == "pending_payment" and exp and exp < now:
                obj.status = "cancelled_expired"
                if not obj.cancel_reason:
                    obj.cancel_reason = "expired"
                obj.version = (obj.version or 1) + 1
                db.add(obj)
        if items:
            db.commit()
        return len(items)
    except Exception:
        # Roll back partial work, then bubble up the error
        db.rollback()
        raise
    finally:
        # Close the session only if this function created it
        if created_session:
            db.close()
