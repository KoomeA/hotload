"""Scheduled recording: scheduled_recordings.

Revision ID: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"


def upgrade() -> None:
    if "scheduled_recordings" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "scheduled_recordings",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), nullable=False, index=True),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("start_at", sa.Float, nullable=False),
        sa.Column("minutes", sa.Integer, nullable=False),
        sa.Column("preset", sa.String(16), nullable=False),
        sa.Column("adult", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("age_confirmed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("state", sa.String(10), nullable=False, server_default="waiting"),
        sa.Column("job_id", sa.String(32), nullable=True),
        sa.Column("message", sa.String(200), nullable=False, server_default=""),
        sa.Column("next_check", sa.Float, nullable=False, server_default="0"),
        sa.Column("created", sa.Float, nullable=False),
    )
    op.create_index("ix_sched_state_start", "scheduled_recordings", ["state", "start_at"])


def downgrade() -> None:
    op.drop_table("scheduled_recordings")
