"""Email verification (users.email_verified).

Revision ID: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    if "email_verified" not in cols:
        op.add_column("users", sa.Column("email_verified", sa.Boolean, nullable=False, server_default=sa.false()))
        # A provider (GitHub/Google) only signs people in with an address it has verified.
        op.execute("UPDATE users SET email_verified = " + ("1" if op.get_bind().dialect.name == "sqlite" else "true")
                   + " WHERE oauth_provider IS NOT NULL")


def downgrade() -> None:
    op.drop_column("users", "email_verified")
