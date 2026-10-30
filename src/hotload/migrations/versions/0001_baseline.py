"""Baseline: users and history (M2). Idempotent so databases created by M2's create_all are adopted as-is.

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def _has(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("email", sa.String(320), nullable=False),
            sa.Column("password_hash", sa.String(255)),
            sa.Column("tier", sa.String(16), nullable=False, server_default="free"),
            sa.Column("oauth_provider", sa.String(16)),
            sa.Column("oauth_sub", sa.String(128)),
            sa.Column("created", sa.Float, nullable=False),
            sa.UniqueConstraint("oauth_provider", "oauth_sub"),
        )
        op.create_index("ix_users_email", "users", ["email"], unique=True)
    if not _has("history"):
        op.create_table(
            "history",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("user_id", sa.String(32), nullable=False),
            sa.Column("url", sa.String(2048), nullable=False),
            sa.Column("title", sa.String(300)),
            sa.Column("site", sa.String(64)),
            sa.Column("preset", sa.String(16), nullable=False),
            sa.Column("state", sa.String(10), nullable=False, server_default="queued"),
            sa.Column("created", sa.Float, nullable=False),
        )
        op.create_index("ix_history_user_id", "history", ["user_id"])
        op.create_index("ix_history_user_created", "history", ["user_id", "created"])


def downgrade() -> None:
    op.drop_table("history")
    op.drop_table("users")
