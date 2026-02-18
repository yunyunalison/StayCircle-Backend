"""add bookings table and timestamps on users/properties

Revision ID: 0f1a2b3c4d5e
Revises: 7b3f5a2c4e9a
Create Date: 2025-12-04 22:05:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0f1a2b3c4d5e"
down_revision: Union[str, None] = "7b3f5a2c4e9a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add UTC-ish timestamp columns to users and properties.
    # Use CURRENT_TIMESTAMP for broad dialect compatibility (SQLite/MySQL).
    op.add_column(
        "users",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )

    op.add_column(
        "properties",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.add_column(
        "properties",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )

    # Bookings table
    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("property_id", sa.Integer(), sa.ForeignKey("properties.id"), nullable=False),
        sa.Column("guest_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'reserved'")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("start_date < end_date", name="ck_bookings_date_range"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    # Indexes to support common queries and overlap checks
    op.create_index("ix_bookings_property_id", "bookings", ["property_id"])
    op.create_index("ix_bookings_guest_id", "bookings", ["guest_id"])
    op.create_index("ix_bookings_start_date", "bookings", ["start_date"])
    op.create_index("ix_bookings_end_date", "bookings", ["end_date"])
    op.create_index("ix_bookings_property_start", "bookings", ["property_id", "start_date"])
    op.create_index("ix_bookings_property_end", "bookings", ["property_id", "end_date"])


def downgrade() -> None:
    # Drop booking indexes and table
    op.drop_index("ix_bookings_property_end", table_name="bookings")
    op.drop_index("ix_bookings_property_start", table_name="bookings")
    op.drop_index("ix_bookings_end_date", table_name="bookings")
    op.drop_index("ix_bookings_start_date", table_name="bookings")
    op.drop_index("ix_bookings_guest_id", table_name="bookings")
    op.drop_index("ix_bookings_property_id", table_name="bookings")
    op.drop_table("bookings")

    # Remove timestamps from properties and users
    op.drop_column("properties", "updated_at")
    op.drop_column("properties", "created_at")
    op.drop_column("users", "updated_at")
    op.drop_column("users", "created_at")
