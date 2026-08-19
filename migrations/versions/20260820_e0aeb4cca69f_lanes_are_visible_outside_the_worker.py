"""lanes are visible outside the worker

Revision ID:
Revises:
Created: 2026-08-20 00:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e0aeb4cca69f"
down_revision: str | None = "c258991c1082"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # The rate controller's state is a dict in the worker's memory and dies
    # with the process, so from the API a quarantined lane looks exactly like
    # an empty queue. Same argument as source_health, one level along.
    op.create_table(
        "lane_health",
        sa.Column("egress", sa.String(length=64), nullable=False),
        sa.Column("lane", sa.String(length=32), nullable=False),
        sa.Column("window", sa.Float(), nullable=False),
        sa.Column("in_flight", sa.Integer(), nullable=False),
        sa.Column("quarantine_streak", sa.Integer(), nullable=False),
        # Wall clock, converted where both readings were available. A monotonic
        # deadline from another process is meaningless here.
        sa.Column("quarantined_until", sa.DateTime(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("egress", "lane"),
    )


def downgrade() -> None:
    op.drop_table("lane_health")
