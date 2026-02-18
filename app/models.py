# SQLAlchemy ORM models for core domain tables (users, properties, bookings, messages).
# Keep business logic out of models; favor services and transactional logic in route handlers/services.
from sqlalchemy import Column, Integer, String, ForeignKey, Date, DateTime, Index, func, Boolean
from sqlalchemy.orm import declarative_mixin

from .db import Base


@declarative_mixin
class TimestampMixin:
    """Common UTC-aware timestamps automatically managed by the database.

    - created_at: set on insert
    - updated_at: set on insert and updated on each modification
    """
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class User(Base, TimestampMixin):
    """Application user account.

    Roles:
    - landlord: can list/manage properties
    - tenant: can book properties and message hosts
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, index=True)  # "landlord" or "tenant"


class Property(Base, TimestampMixin):
    """Rental listing created and managed by a landlord."""
    __tablename__ = "properties"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    title = Column(String, nullable=False)
    price_cents = Column(Integer, nullable=False)
    requires_approval = Column(Boolean, nullable=False, default=False)


class Booking(Base, TimestampMixin):
    """Reservation record for a property.

    Status transitions (simplified):
    requested -> pending_payment -> confirmed
                         └── cancelled / declined / cancelled_expired

    'version' supports optimistic concurrency control for safe, idempotent updates.
    """
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False, index=True)
    guest_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    start_date = Column(Date, nullable=False, index=True)
    end_date = Column(Date, nullable=False, index=True)
    status = Column(String(20), nullable=False, default="requested")
    total_cents = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    payment_intent_id = Column(String(255), nullable=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    cancel_reason = Column(String(255), nullable=True)
    # incremented on each update to support optimistic concurrency if/when used
    version = Column(Integer, nullable=False, default=1)

    # Indexed access patterns: by property/date ranges, status, and expiry for background sweeps
    __table_args__ = (
        Index("ix_bookings_property_start", "property_id", "start_date"),
        Index("ix_bookings_property_end", "property_id", "end_date"),
        Index("ix_bookings_status", "status"),
        Index("ix_bookings_expires_at", "expires_at"),
    )


class Message(Base):
    """Chat message associated with a property conversation."""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    text = Column(String(1000), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Composite index to paginate and filter messages per property in chronological order
    __table_args__ = (
        Index("ix_messages_property_created_at", "property_id", "created_at"),
    )
