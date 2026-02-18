"""Sprint 8: Add payment_intent_id to bookings

Revision ID: 20251212_201100
Revises: 20251210233000
Create Date: 2025-12-12 20:11:00

Notes:
- Adds bookings.payment_intent_id (String(255), nullable) with index for webhook lookup.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20251212_201100"
down_revision: Union[str, None] = "20251210233000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Guarded add for column
    book_cols = {c["name"] for c in inspector.get_columns("bookings")}
    if "payment_intent_id" not in book_cols:
        with op.batch_alter_table("bookings") as batch_op:
            batch_op.add_column(sa.Column("payment_intent_id", sa.String(length=255), nullable=True))

    # Guarded index create
    existing_indexes = {ix["name"] for ix in inspector.get_indexes("bookings")}
    if "ix_bookings_payment_intent_id" not in existing_indexes:
        op.create_index("ix_bookings_payment_intent_id", "bookings", ["payment_intent_id"])


def downgrade() -> None:
    # Drop index if exists
    try:
        op.drop_index("ix_bookings_payment_intent_id", table_name="bookings")
    except Exception:
        # Index might not exist on some backends; ignore
        pass

    # Drop column guarded
    try:
        with op.batch_alter_table("bookings") as batch_op:
            batch_op.drop_column("payment_intent_id")
    except Exception:
        # Some backends may fail if column missing; ignore
        pass
