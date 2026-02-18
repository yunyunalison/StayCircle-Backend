"""Sprint 9A: Add messages table

Revision ID: 20251215_100300
Revises: 20251212_201100
Create Date: 2025-12-15 10:03:00

Notes:
- Introduces messages table for property-level chat history.
- Adds composite index (property_id, created_at) to support timeline queries.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20251215_100300"
down_revision: Union[str, None] = "20251212_201100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = set(inspector.get_table_names())
    if "messages" not in tables:
        op.create_table(
            "messages",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column("property_id", sa.Integer(), sa.ForeignKey("properties.id"), nullable=False),
            sa.Column("sender_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("text", sa.String(length=1000), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    # Ensure indexes exist
    existing_indexes = {ix["name"] for ix in inspector.get_indexes("messages")} if "messages" in tables else set()

    if "ix_messages_property_id" not in existing_indexes:
        op.create_index("ix_messages_property_id", "messages", ["property_id"])
    if "ix_messages_sender_id" not in existing_indexes:
        op.create_index("ix_messages_sender_id", "messages", ["sender_id"])
    if "ix_messages_property_created_at" not in existing_indexes:
        op.create_index("ix_messages_property_created_at", "messages", ["property_id", "created_at"])


def downgrade() -> None:
    # Drop indexes if present
    for ix in ("ix_messages_property_created_at", "ix_messages_sender_id", "ix_messages_property_id"):
        try:
            op.drop_index(ix, table_name="messages")
        except Exception:
            # Ignore if index missing or backend doesn't support
            pass

    # Drop table
    try:
        op.drop_table("messages")
    except Exception:
        # Ignore if table already absent
        pass
