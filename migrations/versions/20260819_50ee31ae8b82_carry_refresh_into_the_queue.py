"""carry refresh into the queue

Revision ID: 50ee31ae8b82
Revises: 6a8b245e9049
Created: 2026-08-19 14:20:56.463538+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "50ee31ae8b82"
down_revision: str | None = "6a8b245e9049"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        # The server default is not decoration and autogenerate does not write
        # it: SQLite refuses `ADD COLUMN ... NOT NULL` outright unless the
        # statement carries a default of its own, so without this the upgrade
        # fails on every database that already exists — empty or not. The same
        # rule is written down in Database._add_column, which learned it first.
        #
        # It is left in place rather than dropped afterwards. Every existing
        # row means a submission that never asked to bypass the cache, which is
        # exactly what 0 says, and a default that stays makes an INSERT written
        # without this column keep working instead of failing later.
        batch_op.add_column(
            sa.Column("refresh", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_column("refresh")
