"""Sprint 7: Booking workflow v1 (approvals + holds)

Revision ID: 20251210_233000
Revises: 0f1a2b3c4d5e
Create Date: 2025-12-10 23:30:00

Notes:
- Adds properties.requires_approval (Boolean, default false, not null).
- Expands bookings with workflow fields:
  - status: requested | pending_payment | confirmed | cancelled | cancelled_expired | declined
  - total_cents (int, not null), currency (char(3), default 'USD'), expires_at (timestamptz), cancel_reason (string)
- Backfills legacy status 'reserved' -> 'confirmed'
- Adds indexes on bookings.status and bookings.expires_at
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20251210233000"
down_revision: Union[str, None] = "0f1a2b3c4d5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_sqlite() -> bool:
    bind = op.get_bind()
    return bind.dialect.name == "sqlite"


def upgrade() -> None:
    # properties.requires_approval (robust on MySQL)
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    prop_cols = {c["name"] for c in inspector.get_columns("properties")}
    if "requires_approval" not in prop_cols:
        if bind.dialect.name == "mysql":
            # Add column only if missing (guarded above)
            op.execute(sa.text("ALTER TABLE properties ADD COLUMN requires_approval BOOL NOT NULL DEFAULT false"))
            # Optional: keep default; altering to drop default is non-essential for Sprint 7
        else:
            with op.batch_alter_table("properties") as batch_op:
                batch_op.add_column(sa.Column("requires_approval", sa.Boolean(), nullable=False, server_default=sa.text("0" if _is_sqlite() else "false")))
            # Drop server_default after backfill to keep column NOT NULL without permanent default
            with op.batch_alter_table("properties") as batch_op:
                batch_op.alter_column("requires_approval", server_default=None)

    # bookings columns (guarded adds)
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    book_cols = {c["name"] for c in inspector.get_columns("bookings")}
    with op.batch_alter_table("bookings") as batch_op:
        # Add new workflow columns with temporary defaults for backfill-safety, only if missing
        if "total_cents" not in book_cols:
            batch_op.add_column(sa.Column("total_cents", sa.Integer(), nullable=False, server_default=sa.text("0")))
        if "currency" not in book_cols:
            batch_op.add_column(sa.Column("currency", sa.String(length=3), nullable=False, server_default=sa.text("'USD'")))
        if "expires_at" not in book_cols:
            batch_op.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
        if "cancel_reason" not in book_cols:
            batch_op.add_column(sa.Column("cancel_reason", sa.String(length=255), nullable=True))

        # Remove old server default 'reserved' on status and optionally change default to requested (optional)
        # SQLite doesn't support altering existing server_default directly; batch_alter_table handles it.
        batch_op.alter_column("status", existing_type=sa.String(length=20), server_default=None, existing_nullable=False)

    # Backfill legacy status 'reserved' -> 'confirmed'
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE bookings SET status = 'confirmed' WHERE status = 'reserved'"))

    # Remove temporary defaults on new columns if we added them (keep currency default 'USD' per plan)
    if "total_cents" not in book_cols:
        with op.batch_alter_table("bookings") as batch_op:
            batch_op.alter_column("total_cents", server_default=None, existing_type=sa.Integer())
        # If you prefer ORM default only for currency, you could also drop default similarly.

    # New indexes (guarded)
    existing_indexes = {ix["name"] for ix in inspector.get_indexes("bookings")}
    if "ix_bookings_status" not in existing_indexes:
        op.create_index("ix_bookings_status", "bookings", ["status"])
    if "ix_bookings_expires_at" not in existing_indexes:
        op.create_index("ix_bookings_expires_at", "bookings", ["expires_at"])


def downgrade() -> None:
    # Drop indexes
    op.drop_index("ix_bookings_expires_at", table_name="bookings")
    op.drop_index("ix_bookings_status", table_name="bookings")

    # Drop new booking columns
    with op.batch_alter_table("bookings") as batch_op:
        batch_op.drop_column("cancel_reason")
        batch_op.drop_column("expires_at")
        batch_op.drop_column("currency")
        batch_op.drop_column("total_cents")

        # Restore a conservative server_default on status for legacy behavior
        batch_op.alter_column("status", existing_type=sa.String(length=20), server_default=sa.text("'reserved'"), existing_nullable=False)

    # Drop requires_approval from properties
    with op.batch_alter_table("properties") as batch_op:
        batch_op.drop_column("requires_approval")
