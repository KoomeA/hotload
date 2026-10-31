"""Refunds and chargebacks: payment audit trail (payment_events); payments.status widened for the new states.

Revision ID: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"


def upgrade() -> None:
    bind = op.get_bind()
    if "payment_events" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "payment_events",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("payment_id", sa.String(32), nullable=False, index=True),
            sa.Column("at", sa.Float, nullable=False),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("actor", sa.String(64), nullable=False),
            sa.Column("from_status", sa.String(12), nullable=False),
            sa.Column("to_status", sa.String(12), nullable=False),
            sa.Column("entitlement", sa.String(32), nullable=False, server_default="none"),
            sa.Column("note", sa.String(300), nullable=False, server_default=""),
        )
    if bind.dialect.name != "sqlite":  # SQLite does not enforce VARCHAR lengths; PostgreSQL does ("chargedback" is 11)
        op.alter_column("payments", "status", type_=sa.String(12), existing_type=sa.String(10), existing_nullable=False)


def downgrade() -> None:
    op.drop_table("payment_events")
