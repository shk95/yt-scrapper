"""Carrying the index across, and proving it arrived.

`#15` says `- [ ] Data across: six tables` and `#24` says `2. 데이터를 옮긴다.
여섯 테이블.` — that is the entire specification. Nothing in this repository
has ever moved a row from one database to another, and the rows in question
include 248 targets whose repeated observations are not re-collectable at any
price. These tests are the proof that a transfer actually carries every row
of every table, verbatim, rather than a `pg_dump`-and-hope run by hand.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from tubedepth.database import Database
from tubedepth.errors import ConfigurationError
from tubedepth.models import (
    ApiKey,
    Artifact,
    Job,
    LaneHealth,
    SourceHealth,
    WorkerControl,
)
from tubedepth.transfer import transfer

FETCHED_AT = datetime(2026, 7, 30, 3, 4, 5, 123456, tzinfo=UTC)


def _seeded(path: Path) -> Database:
    """A source database holding a representative row in each of the six
    tables — including a job whose `identifier` the test can look for again
    after the crossing, and an artifact whose `fetched_at` carries
    microseconds, since a round instant would hide a truncating transfer."""
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    with database.session() as session:
        session.add_all(
            [
                Job(identifier="job-one", kind="video.metadata", target="a"),
                Job(identifier="job-two", kind="video.metadata", target="b", refresh=True),
                Job(identifier="job-three", kind="channel.videos", target="c"),
                Artifact(
                    identifier="artifact-one",
                    kind="video.metadata",
                    target="a",
                    fingerprint="fp-a",
                    digest="d-a",
                    byte_count=1,
                    fetched_at=FETCHED_AT,
                    fresh_until=FETCHED_AT + timedelta(hours=6),
                ),
                Artifact(
                    identifier="artifact-two",
                    kind="video.metadata",
                    target="a",
                    fingerprint="fp-a",
                    digest="d-a2",
                    byte_count=2,
                    fetched_at=FETCHED_AT + timedelta(hours=1),
                    fresh_until=FETCHED_AT + timedelta(hours=7),
                ),
                Artifact(
                    identifier="artifact-three",
                    kind="channel.about",
                    target="c",
                    fingerprint="fp-c",
                    digest="d-c",
                    byte_count=3,
                    fetched_at=FETCHED_AT + timedelta(hours=2),
                    fresh_until=FETCHED_AT + timedelta(hours=8),
                ),
                Artifact(
                    identifier="artifact-four",
                    kind="video.metadata",
                    target="b",
                    fingerprint="fp-b",
                    digest="d-b",
                    byte_count=4,
                    fetched_at=FETCHED_AT + timedelta(hours=3),
                    fresh_until=FETCHED_AT + timedelta(hours=9),
                ),
                ApiKey(
                    identifier="key-one",
                    label="ops",
                    key_prefix="td_abcdefgh",
                    key_hash="hash",
                ),
                WorkerControl(identifier="worker", paused=True, reason="maintenance"),
                LaneHealth(egress="direct", lane="video"),
                LaneHealth(egress="direct", lane="channel"),
                SourceHealth(kind="video.metadata"),
                SourceHealth(kind="channel.about", blocked=True),
            ]
        )
    return database


def _fields(row: object) -> dict[str, object]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}  # type: ignore[attr-defined]


def test_every_row_of_every_table_arrives(tmp_path: Path) -> None:
    """Six tables, and the count is asserted per table rather than in total.

    A total hides the failure that matters: five tables arriving and one
    silently empty reads as "moved 12 rows" either way.
    """
    source = _seeded(tmp_path / "source.db")
    target = Database(f"sqlite+pysqlite:///{tmp_path / 'target.db'}")
    target.create_schema()

    outcome = transfer(source=source, target=target)

    assert outcome.rows == {
        "jobs": 3,
        "artifacts": 4,
        "api_keys": 1,
        "worker_control": 1,
        "lane_health": 2,
        "source_health": 2,
    }


def test_an_observations_instant_survives_to_the_microsecond(tmp_path: Path) -> None:
    """The x-axis of the time series. A transfer that rounds it has destroyed
    the thing the transfer exists to protect."""
    source = _seeded(tmp_path / "source.db")
    target = Database(f"sqlite+pysqlite:///{tmp_path / 'target.db'}")
    target.create_schema()

    transfer(source=source, target=target)

    with source.session(readonly=True) as reading:
        original = reading.get(Artifact, "artifact-one")
        assert original is not None

    with target.session(readonly=True) as reading:
        moved = reading.get(Artifact, "artifact-one")
        assert moved is not None

    assert moved.fetched_at == original.fetched_at
    assert moved.fetched_at.microsecond == 123456
    assert moved.fetched_at.tzinfo is not None


def test_identifiers_are_carried_rather_than_regenerated(tmp_path: Path) -> None:
    """`jobs.payload_digest` and `GET /v1/jobs/{id}/result` address by these."""
    source = _seeded(tmp_path / "source.db")
    target = Database(f"sqlite+pysqlite:///{tmp_path / 'target.db'}")
    target.create_schema()

    transfer(source=source, target=target)

    with target.session(readonly=True) as reading:
        identifiers = {job.identifier for job in reading.scalars(select(Job)).all()}

    assert identifiers == {"job-one", "job-two", "job-three"}


def test_a_target_that_already_holds_rows_is_refused(tmp_path: Path) -> None:
    """`artifacts` has no unique constraint on `fingerprint` — deliberately,
    because observations accumulate — so a second run would duplicate every
    observation and nothing would complain."""
    source = _seeded(tmp_path / "source.db")
    target = Database(f"sqlite+pysqlite:///{tmp_path / 'target.db'}")
    target.create_schema()
    with target.session() as session:
        session.add(Job(identifier="already-here", kind="video.metadata", target="z"))

    with pytest.raises(ConfigurationError) as refused:
        transfer(source=source, target=target)
    assert "already holds" in str(refused.value)

    # Refused before anything else was written, not just for the table that
    # already held rows.
    with target.session(readonly=True) as reading:
        assert reading.scalar(select(Artifact)) is None


def test_the_transfer_does_not_touch_the_payload_store(tmp_path: Path) -> None:
    """Rule 7: the index and the bytes are a pair, and this moves one of them.

    Asserted by construction — the module must not be able to reach the store.
    """
    import tubedepth.transfer as module

    assert "PayloadStore" not in dir(module)


# --- PostgreSQL round trip -------------------------------------------------

MIGRATOR_URL = os.environ.get("TUBEDEPTH_TEST_POSTGRES_URL")
RUNTIME_URL = os.environ.get("TUBEDEPTH_TEST_POSTGRES_RUNTIME_URL")
needs_postgres = pytest.mark.skipif(
    not MIGRATOR_URL or not RUNTIME_URL,
    reason="set TUBEDEPTH_TEST_POSTGRES_URL and _RUNTIME_URL, or run `just postgres`",
)


def _key(row: object) -> tuple[object, ...]:
    """A row with no single-column identifier is addressed by its primary key."""
    table = row.__table__  # type: ignore[attr-defined]
    return tuple(getattr(row, column.name) for column in table.primary_key.columns)


def _row_key(row: object) -> object:
    """`identifier` when the row has one; its full primary key otherwise
    (`LaneHealth` and `SourceHealth` are keyed by composite/non-`identifier`
    columns)."""
    identifier = getattr(row, "identifier", None)
    return identifier if identifier is not None else _key(row)


@pytest.mark.postgres
@needs_postgres
def test_a_sqlite_index_round_trips_through_postgresql(tmp_path: Path) -> None:
    """SQLite in, PostgreSQL out, and every field compared.

    This is the check #15 and #24 both assume exists. It compares every column
    of every row rather than counting, because the failures that matter here
    are type failures — an aware instant arriving naive, a bool arriving as 0,
    an enum arriving as its name rather than its value.

    The target opens as `tubedepth_runtime`, not the migrator: rule 1 makes
    the migrator `NOINHERIT` with no direct grant on `tubedepth`'s tables — it
    only acts as the owner through `migrations/env.py`'s explicit `SET ROLE`
    — so a plain connection as the migrator cannot `INSERT` at all. Runtime is
    the only role actually granted `SELECT, INSERT, UPDATE, DELETE`, which is
    exactly the DML transfer needs and nothing more.
    """
    from sqlalchemy import create_engine, text

    schema = Database.SCHEMA
    engine = create_engine(MIGRATOR_URL or "")
    with engine.begin() as connection:
        connection.execute(text("SET ROLE tubedepth_owner"))
        connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        connection.execute(text("RESET ROLE"))
        connection.execute(text(f"CREATE SCHEMA {schema} AUTHORIZATION tubedepth_owner"))
    engine.dispose()

    import subprocess
    import sys

    root = Path(__file__).parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "TUBEDEPTH_DATABASE_URL": MIGRATOR_URL or "",
            "PYTHONPATH": str(root / "src"),
        },
    )
    assert result.returncode == 0, result.stderr

    engine = create_engine(MIGRATOR_URL or "")
    with engine.begin() as connection:
        connection.execute(text("SET ROLE tubedepth_owner"))
        connection.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO tubedepth_runtime"))
        connection.execute(
            text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} "
                "TO tubedepth_runtime"
            )
        )
        connection.execute(
            text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO tubedepth_runtime")
        )
    engine.dispose()

    source = _seeded(tmp_path / "source.db")
    target = Database(RUNTIME_URL or "")

    outcome = transfer(source=source, target=target)
    assert sum(outcome.rows.values()) > 0, (
        "the source had rows and the transfer must have moved some"
    )

    for model in (Job, Artifact, ApiKey, WorkerControl, LaneHealth, SourceHealth):
        with source.session(readonly=True) as before, target.session(readonly=True) as after:
            before_rows = before.scalars(select(model)).all()
            after_rows = after.scalars(select(model)).all()
            assert before_rows, (
                f"{model.__tablename__} had no rows in the source; this proves nothing"
            )
            expected = {_row_key(row): _fields(row) for row in before_rows}
            actual = {_row_key(row): _fields(row) for row in after_rows}
        assert actual == expected, f"{model.__tablename__} did not survive the crossing"
