"""M3: plan expiry on users, payments, API keys, blocklist, abuse reports.

Revision ID: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def _has(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    if "tier_expires" not in cols:
        op.add_column("users", sa.Column("tier_expires", sa.Float, nullable=True))
    if not _has("payments"):
        op.create_table(
            "payments",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("user_id", sa.String(32), nullable=False),
            sa.Column("provider", sa.String(16), nullable=False),
            sa.Column("plan", sa.String(24), nullable=False),
            sa.Column("amount_minor", sa.Integer, nullable=False),
            sa.Column("currency", sa.String(3), nullable=False),
            sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
            sa.Column("provider_ref", sa.String(128)),
            sa.Column("created", sa.Float, nullable=False),
            sa.Column("paid_at", sa.Float),
        )
        op.create_index("ix_payments_user_id", "payments", ["user_id"])
        op.create_index("ix_payments_provider_ref", "payments", ["provider_ref"])
    if not _has("api_keys"):
        op.create_table(
            "api_keys",
            sa.Column("id", sa.String(16), primary_key=True),
            sa.Column("user_id", sa.String(32), nullable=False),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("webhook_secret", sa.String(64), nullable=False),
            sa.Column("created", sa.Float, nullable=False),
            sa.Column("last_used", sa.Float),
            sa.Column("revoked", sa.Boolean, nullable=False, server_default=sa.false()),
        )
        op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    if not _has("blocklist"):
        op.create_table(
            "blocklist",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("kind", sa.String(8), nullable=False),
            sa.Column("value", sa.String(2048), nullable=False),
            sa.Column("reason", sa.String(200), nullable=False, server_default=""),
            sa.Column("created", sa.Float, nullable=False),
            sa.Column("created_by", sa.String(32)),
            sa.UniqueConstraint("kind", "value"),
        )
    if not _has("reports"):
        op.create_table(
            "reports",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("url", sa.String(2048), nullable=False),
            sa.Column("kind", sa.String(12), nullable=False),
            sa.Column("contact", sa.String(320), nullable=False),
            sa.Column("details", sa.String(2000), nullable=False, server_default=""),
            sa.Column("status", sa.String(10), nullable=False, server_default="open"),
            sa.Column("created", sa.Float, nullable=False),
            sa.Column("resolved", sa.Float),
        )


def downgrade() -> None:
    for table in ("reports", "blocklist", "api_keys", "payments"):
        op.drop_table(table)
    op.drop_column("users", "tier_expires")
