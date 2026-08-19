"""an operator can pause the worker

Revision ID: 1d55f4476c01
Revises: e0aeb4cca69f
Created: 2026-08-20 00:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "1d55f4476c01"
down_revision: str | None = "e0aeb4cca69f"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # The API and the worker are separate processes on purpose, so nothing in
    # one can reach into the other. A row is the channel they already share —
    # source_health and lane_health run the other way down it.
    op.create_table(
        "worker_control",
        sa.Column("identifier", sa.String(length=32), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("identifier"),
    )


def downgrade() -> None:
    op.drop_table("worker_control")
