"""artifacts name their schema version

Revision ID: c258991c1082
Revises: 50ee31ae8b82
Created: 2026-08-20 00:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c258991c1082"
down_revision: str | None = "50ee31ae8b82"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        # Nullable, and deliberately without a server default. The `refresh`
        # migration needed one because SQLite refuses `ADD COLUMN ... NOT NULL`
        # without it; a nullable column is under no such rule, and a default
        # here would be worse than absent — it would make every existing row
        # assert a version nobody recorded, and `channel.about` is already at
        # "2", so "1" would be wrong for exactly the rows whose contents are
        # known to be wrong. Null is what the backfill selects on.
        batch_op.add_column(sa.Column("schema_version", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.drop_column("schema_version")
