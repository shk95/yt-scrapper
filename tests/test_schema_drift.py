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
        connection.execute("ALTER TABLE jobs DROP COLUMN kind")

    with pytest.raises(ConfigurationError) as caught:
        Database(path).create_schema()

    assert "jobs.kind" in str(caught.value)


def test_a_database_created_by_this_version_needs_no_repair(tmp_path: Path) -> None:
    path = tmp_path / "tubedepth.db"
    Database(path).create_schema()
    Database(path).create_schema()

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    assert columns == {column.name for column in Job.__table__.columns}
