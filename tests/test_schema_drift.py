"""A database file that predates a column the code now writes.

There is no migration tool here yet, and `create_all` is silent about the one
case that actually happens during development: a table that already exists but
is missing a column added since. SQLAlchemy skips such a table entirely, so the
schema looks created and the first INSERT fails deep inside a worker with
`table jobs has no column named api_key_id` — hours after the deploy that
caused it, and nowhere near the change.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tubedepth.database import Database
from tubedepth.errors import ConfigurationError
from tubedepth.models import Job


def test_a_column_added_since_the_file_was_created_is_added_to_the_existing_table(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tubedepth.db"
    Database(path).create_schema()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE jobs DROP COLUMN api_key_id")

    Database(path).create_schema()

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    assert "api_key_id" in columns


def test_a_missing_column_that_cannot_be_added_is_reported_by_name(tmp_path: Path) -> None:
    """A NOT NULL column with no default cannot be backfilled by us.

    Refusing by name beats a half-migrated file: the operator learns which
    column and which table, which is the whole content of the fix.
    """
    path = tmp_path / "tubedepth.db"
    Database(path).create_schema()
    with sqlite3.connect(path) as connection:
        # `artifacts.byte_count`, chosen for what it is rather than which
        # table it is in: required, no default, and in no index. An indexed
        # column cannot be dropped at all, and a column with a default is one
        # the repair can legitimately rebuild.
        connection.execute("ALTER TABLE artifacts DROP COLUMN byte_count")

    with pytest.raises(ConfigurationError) as caught:
        Database(path).create_schema()

    assert "artifacts.byte_count" in str(caught.value)


def test_a_database_created_by_this_version_needs_no_repair(tmp_path: Path) -> None:
    path = tmp_path / "tubedepth.db"
    Database(path).create_schema()
    Database(path).create_schema()

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    assert columns == {column.name for column in Job.__table__.columns}


def test_an_index_added_since_the_file_was_created_is_created_too(tmp_path: Path) -> None:
    """The same gap as the missing column, one level down.

    `create_all` skips a table it already finds, and that skips the table's
    indexes with it. So an index added after a database exists never lands on
    it — silently, because an index is a performance decision and nothing
    errors without one. The claim query went back to a full scan on the working
    database while every test asserted it used an index.
    """
    path = tmp_path / "tubedepth.db"
    Database(path).create_schema()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX ix_job_claimable")

    Database(path).create_schema()

    with sqlite3.connect(path) as connection:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert "ix_job_claimable" in names


def test_a_required_column_with_a_known_default_is_added_with_it(tmp_path: Path) -> None:
    """SQLite refuses `ADD COLUMN ... NOT NULL` without a *server* default.

    The repair checked `column.default`, which is SQLAlchemy's Python-side
    default applied at INSERT — a different thing, and invisible to ALTER. So
    adding `webhook_attempts INTEGER NOT NULL DEFAULT 0` looked repairable,
    passed the guard, and failed on the statement. The database then refused to
    open at all, which is how it was found: the server would not start.

    A scalar default is enough to fill in existing rows, so the repair supplies
    it rather than refusing.
    """
    path = tmp_path / "tubedepth.db"
    Database(path).create_schema()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE jobs DROP COLUMN webhook_attempts")

    Database(path).create_schema()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO jobs (identifier, kind, target, state, attempt_count, max_attempts,"
            " scheduled_at, created_at) VALUES ('a', 'k', 't', 'queued', 0, 3, 'x', 'y')"
        )
        value = next(connection.execute("SELECT webhook_attempts FROM jobs"))[0]
    assert value == 0, "existing rows were left without a value for a NOT NULL column"
