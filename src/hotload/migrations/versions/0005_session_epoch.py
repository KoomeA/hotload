"""Launch review: revocable sessions (users.session_epoch).

Revision ID: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    if "session_epoch" not in cols:
        op.add_column("users", sa.Column("session_epoch", sa.Integer, nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("users", "session_epoch")
