"""`Database`'s dialect-conditional behaviour, against a real PostgreSQL server.

`tests/test_database_url.py` proves the wiring offline: a PostgreSQL URL
installs no SQLite pragmas. It cannot prove the PostgreSQL-only half actually
works, because SQLite is the only dialect the offline suite is allowed to
assume is there. `_install_read_only_hook` implements the one shape
`docs/shared-postgres.md` says must not exist — a session that declares
`readonly=True` and then writes — and until this file, that guarantee was
checked only by an ad hoc, uncommitted script. `just check` cannot catch a
regression here at all: SQLite takes the other branch in `Database.__init__`
and never reaches this code.

Marked `postgres` and deselected by default, for the same reason
`test_postgres_migrations.py` is: it needs a server the offline suite must not
assume. `just postgres` starts one and runs this.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tubedepth.database import Database
from tubedepth.models import Job

SCHEMA = "tubedepth"

pytestmark = pytest.mark.postgres

URL = os.environ.get("TUBEDEPTH_TEST_POSTGRES_URL")
needs_postgres = pytest.mark.skipif(
    not URL, reason="set TUBEDEPTH_TEST_POSTGRES_URL, or run `just postgres`"
)


@pytest.fixture
def database() -> Iterator[Database]:
    """A migrated schema, dropped and rebuilt for the same reason
    `test_postgres_migrations.py`'s `empty_database` is: a failure partway
    through one test must not decide what the next one starts from."""
    engine = create_engine(URL or "")
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    engine.dispose()

    database = Database(URL or "")
    database.create_schema()
    yield database


@needs_postgres
def test_the_dialect_is_postgresql_and_no_sqlite_hooks_are_installed(
    database: Database,
) -> None:
    assert database.dialect == "postgresql"
    assert database.sqlite_hooks_installed is False


@needs_postgres
def test_a_readonly_session_refuses_to_write(database: Database) -> None:
    """The guarantee `_install_read_only_hook` exists to provide.

    On SQLite this is `PRAGMA query_only`; here it is
    `SET TRANSACTION READ ONLY`, issued on the read engine's `begin` event.
    Without it, `readonly=True` is a hint nothing enforces — a session that
    claims to only read and then writes is exactly the shape that must not
    exist, since two of them can interleave the way SQLite's IMMEDIATE write
    lock was added to prevent, and PostgreSQL has no equivalent up-front lock
    to catch it instead.
    """
    with database.session() as session:
        session.add(Job(kind="video.metadata", target="dQw4w9WgXcQ"))

    with (
        pytest.raises(DBAPIError, match="read-only transaction"),
        database.session(readonly=True) as session,
    ):
        session.add(Job(kind="video.metadata", target="written-while-readonly"))
        session.flush()


@needs_postgres
def test_a_readonly_session_can_still_read(database: Database) -> None:
    """The other half: `readonly=True` must not also refuse the reads it
    exists to serve."""
    with database.session() as session:
        session.add(Job(kind="video.metadata", target="dQw4w9WgXcQ"))

    with database.session(readonly=True) as session:
        jobs = session.query(Job).all()

    assert [job.target for job in jobs] == ["dQw4w9WgXcQ"]
