"""The indexes the queue and the record browser both depend on.

`jobs` had none at all except its primary key, so every claim was a full scan
plus a temporary B-tree for the ordering. At five hundred rows that is
invisible; a browser filtering by kind and date over a hundred thousand is not,
and the claim is on the hot path of every job the system ever runs.

These assert the query planner's choice rather than a duration. A timing test
measures the machine it runs on; `EXPLAIN` measures the decision.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from tubedepth.database import Database
from tubedepth.models import Artifact, Job, JobState, utcnow

ROW_COUNT = 2000


@pytest.fixture
def seeded(database: Database) -> Database:
    """Enough rows that PostgreSQL's cost-based planner actually prefers an
    index over a sequential scan.

    On an empty table the planner always prefers the scan — index or not —
    since there is nothing for an index to save it from reading. Without real
    rows, every test below would pass whether or not the index it claims to
    check even existed, which is worse than not having the test at all.
    `ANALYZE` afterwards is what gives the planner real statistics to cost
    the alternatives against, rather than the defaults it assumes for a table
    it has never seen data in.
    """
    now = utcnow()
    with database.session() as session:
        for index in range(ROW_COUNT):
            claimed = index % 7 == 0
            session.add(
                Job(
                    kind=f"kind.{index % 20}",
                    target=f"target-{index}",
                    state=JobState.RUNNING if claimed else JobState.QUEUED,
                    scheduled_at=now - timedelta(minutes=index),
                    created_at=now - timedelta(minutes=index),
                    lease_expires_at=(now - timedelta(minutes=index)) if claimed else None,
                )
            )
            session.add(
                Artifact(
                    kind=f"kind.{index % 20}",
                    target=f"target-{index % 500}",
                    fingerprint=f"fingerprint-{index}",
                    digest=f"{index:064d}",
                    byte_count=1,
                    fetched_at=now - timedelta(minutes=index),
                    fresh_until=now,
                )
            )
    with database.session() as session:
        session.execute(text("ANALYZE jobs"))
        session.execute(text("ANALYZE artifacts"))
    return database


def plan(database: Database, sql: str) -> str:
    with database.session(readonly=True) as session:
        rows = session.execute(text(f"EXPLAIN {sql}")).all()
    return " | ".join(str(row[0]) for row in rows)


def test_the_claim_query_uses_an_index_rather_than_scanning(seeded: Database) -> None:
    """The hot path of the whole system: every job starts here."""
    chosen = plan(
        seeded,
        "SELECT identifier FROM jobs WHERE state = 'queued' AND scheduled_at <= now() "
        "ORDER BY scheduled_at, created_at LIMIT 1",
    )

    assert "Seq Scan" not in chosen, chosen
    assert "Index" in chosen, chosen


def test_the_reaper_query_uses_an_index(seeded: Database) -> None:
    chosen = plan(
        seeded,
        "SELECT identifier FROM jobs WHERE state = 'running' AND lease_expires_at < now()",
    )

    assert "Seq Scan" not in chosen, chosen


def test_browsing_jobs_by_kind_and_time_uses_an_index(seeded: Database) -> None:
    """What the record browser asks for, and the reason it is not just the
    claim index: browsing filters on kind and orders by recency."""
    chosen = plan(
        seeded,
        "SELECT identifier FROM jobs WHERE kind = 'kind.5' ORDER BY created_at DESC LIMIT 50",
    )

    assert "Seq Scan" not in chosen, chosen


def test_browsing_artifacts_by_kind_and_time_uses_an_index(seeded: Database) -> None:
    chosen = plan(
        seeded,
        "SELECT identifier FROM artifacts WHERE kind = 'kind.5' ORDER BY fetched_at DESC LIMIT 50",
    )

    assert "Seq Scan" not in chosen, chosen


def test_looking_up_every_artifact_for_one_target_uses_an_index(seeded: Database) -> None:
    """A video's history over time, which is the thing the artifact table keeps
    rather than overwriting."""
    chosen = plan(
        seeded,
        "SELECT identifier FROM artifacts WHERE target = 'target-5' ORDER BY fetched_at DESC",
    )

    assert "Seq Scan" not in chosen, chosen
