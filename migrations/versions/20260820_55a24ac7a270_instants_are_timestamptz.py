"""instants are timestamptz

Revision ID: 55a24ac7a270
Revises: 1d55f4476c01
Created: 2026-08-20 00:00:00.000000+00:00
"""

from __future__ import annotations

from alembic import op

revision: str = "55a24ac7a270"
down_revision: str | None = "1d55f4476c01"
branch_labels: str | None = None
depends_on: str | None = None

# The full, explicit set of `UtcDateTime` columns as of this revision — taken
# from `tubedepth.models`, not derived from it at migration time. A migration
# that reads application code breaks when the application is refactored
# (`migrations/env.py`'s own docstring says so), and this list is the reason
# that docstring gives.
COLUMNS = (
    ("jobs", "scheduled_at"),
    ("jobs", "created_at"),
    ("jobs", "lease_expires_at"),
    ("jobs", "finished_at"),
    ("jobs", "cancel_requested_at"),
    ("jobs", "webhook_delivered_at"),
    ("artifacts", "fetched_at"),
    ("artifacts", "fresh_until"),
    ("api_keys", "created_at"),
    ("api_keys", "last_used_at"),
    ("api_keys", "revoked_at"),
    ("worker_control", "changed_at"),
    ("lane_health", "quarantined_until"),
    ("lane_health", "observed_at"),
    ("source_health", "last_success_at"),
    ("source_health", "last_failure_at"),
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        # SQLite has no timezone-aware storage either way, so a value written
        # there is stored the same regardless of this flag — there is nothing
        # to alter, and `render_item`'s SQLite chain stays untouched.
        return
    for table, column in COLUMNS:
        # `AT TIME ZONE 'UTC'` is load-bearing, not decorative: without it
        # PostgreSQL reinterprets the stored wall-clock value in the session's
        # timezone rather than treating it as UTC. Every value this
        # application ever wrote is UTC by construction —
        # `UtcDateTime.process_bind_param` converts to UTC and refuses naive
        # input — so reading the existing wall-clock value as UTC is the only
        # correct interpretation. Dropping this clause would silently shift
        # every stored instant by the session's offset.
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE timestamptz USING {column} AT TIME ZONE 'UTC'"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, column in COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE timestamp")
