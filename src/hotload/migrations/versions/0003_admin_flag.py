"""M3: admin flag on users (set from the server CLI only).

Revision ID: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    if "is_admin" not in cols:
        op.add_column("users", sa.Column("is_admin", sa.Boolean, nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("users", "is_admin")
