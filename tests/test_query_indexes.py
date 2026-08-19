"""The indexes the queue and the record browser both depend on.

`jobs` had none at all except its primary key, so every claim was a full scan
plus a temporary B-tree for the ordering. At five hundred rows that is
invisible; a browser filtering by kind and date over a hundred thousand is not,
and the claim is on the hot path of every job the system ever runs.

These assert the query planner's choice rather than a duration. A timing test
measures the machine it runs on; `EXPLAIN QUERY PLAN` measures the decision.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from tubedepth.database import Database


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    return database


def plan(database: Database, sql: str) -> str:
    with database.session(readonly=True) as session:
        rows = session.execute(text(f"EXPLAIN QUERY PLAN {sql}")).all()
    return " | ".join(str(row[3]) for row in rows)


def test_the_claim_query_uses_an_index_rather_than_scanning(database: Database) -> None:
    """The hot path of the whole system: every job starts here."""
    chosen = plan(
        database,
        "SELECT identifier FROM jobs WHERE state = 'queued' AND scheduled_at <= '2026-08-19' "
        "ORDER BY scheduled_at, created_at LIMIT 1",
    )

    assert "SCAN jobs" not in chosen, chosen
    assert "USING INDEX" in chosen, chosen


def test_the_reaper_query_uses_an_index(database: Database) -> None:
    chosen = plan(
        database,
        "SELECT identifier FROM jobs WHERE state = 'running' AND lease_expires_at < '2026-08-19'",
    )

    assert "SCAN jobs" not in chosen, chosen


def test_browsing_jobs_by_kind_and_time_uses_an_index(database: Database) -> None:
    """What the record browser asks for, and the reason it is not just the
    claim index: browsing filters on kind and orders by recency."""
    chosen = plan(
        database,
        "SELECT identifier FROM jobs WHERE kind = 'video.metadata' "
        "ORDER BY created_at DESC LIMIT 50",
    )

    assert "SCAN jobs" not in chosen, chosen


def test_browsing_artifacts_by_kind_and_time_uses_an_index(database: Database) -> None:
    chosen = plan(
        database,
        "SELECT identifier FROM artifacts WHERE kind = 'video.metadata' "
        "ORDER BY fetched_at DESC LIMIT 50",
    )

    assert "SCAN artifacts" not in chosen, chosen


def test_looking_up_every_artifact_for_one_target_uses_an_index(database: Database) -> None:
    """A video's history over time, which is the thing the artifact table keeps
    rather than overwriting."""
    chosen = plan(
        database,
        "SELECT identifier FROM artifacts WHERE target = 'dQw4w9WgXcQ' ORDER BY fetched_at DESC",
    )

    assert "SCAN artifacts" not in chosen, chosen
