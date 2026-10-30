"""M4: encrypted per-user site cookies.

Revision ID: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("user_cookies"):
        op.create_table(
            "user_cookies",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.String(32), nullable=False),
            sa.Column("domain", sa.String(255), nullable=False),
            sa.Column("blob", sa.String(200000), nullable=False),
            sa.Column("count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("created", sa.Float, nullable=False),
            sa.UniqueConstraint("user_id", "domain"),
        )
        op.create_index("ix_user_cookies_user_id", "user_cookies", ["user_id"])


def downgrade() -> None:
    op.drop_table("user_cookies")
