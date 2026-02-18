"""initial schema: users, properties

Revision ID: 7b3f5a2c4e9a
Revises: 
Create Date: 2025-11-28 18:59:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7b3f5a2c4e9a"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Users table
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.UniqueConstraint("email", name="uq_users_email"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_users_id", "users", ["id"])
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_role", "users", ["role"])

    # Properties table
    op.create_table(
        "properties",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        # Title: model uses String without explicit length; map to Text for cross-dialect compatibility
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_properties_id", "properties", ["id"])
    op.create_index("ix_properties_owner_id", "properties", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_properties_owner_id", table_name="properties")
    op.drop_index("ix_properties_id", table_name="properties")
    op.drop_table("properties")

    op.drop_index("ix_users_role", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("ix_users_id", table_name="users")
    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.drop_table("users")
