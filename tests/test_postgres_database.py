"""`Database`'s read-only guarantee, against a real PostgreSQL server.

`tests/test_database_url.py` proves the wiring: `Database` refuses anything
that is not PostgreSQL (with one deliberate exception, the transfer source),
and reports the right dialect for both. It cannot prove the read-only hook
actually works, because that needs a live server to write against.
`_install_read_only_hook` implements the one shape `docs/shared-postgres.md`
says must not exist — a session that declares `readonly=True` and then writes
— and until this file, that guarantee was checked only by an ad hoc,
uncommitted script.

Marked `postgres`, the label for structural PostgreSQL tests (see
`pyproject.toml`) rather than a selection filter — `tool/checks/test` and CI
both bring up a server and run every test in the suite against it now,
this file included.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tubedepth.database import Database
from tubedepth.models import Base, Job

SCHEMA = "tubedepth"

pytestmark = pytest.mark.postgres

# The migrator's URL builds the schema. `Database` itself, though, is
# ordinary application traffic — in production that always runs as
# tubedepth_runtime, which is the only role granted DML on tubedepth's tables
# and USAGE on the schema, so it is also the only role whose unqualified
# reflection or search_path-implicit access to tubedepth works outside of
# migrations/env.py's `SET ROLE`.
URL = os.environ.get("TUBEDEPTH_TEST_POSTGRES_URL")
RUNTIME_URL = os.environ.get("TUBEDEPTH_TEST_POSTGRES_RUNTIME_URL")
needs_postgres = pytest.mark.skipif(
    not URL or not RUNTIME_URL,
    reason="set TUBEDEPTH_TEST_POSTGRES_URL and _RUNTIME_URL, or run `just postgres`",
)


@pytest.fixture
def database() -> Iterator[Database]:
    """A migrated schema, dropped and rebuilt for the same reason
    `test_postgres_migrations.py`'s `empty_database` is: a failure partway
    through one test must not decide what the next one starts from."""
    engine = create_engine(URL or "")
    with engine.begin() as connection:
        # SET ROLE first: only the owner (or a superuser) can drop a
        # schema it owns; the migrator only gets owner privileges through
        # an explicit SET ROLE (rule 1's NOINHERIT), not automatically via
        # membership. RESET ROLE before CREATE SCHEMA: creating a schema
        # needs CREATE on the database, which is granted to the migrator
        # (harness-only) and not to the owner role.
        connection.execute(text("SET ROLE tubedepth_owner"))
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.execute(text("RESET ROLE"))
        # AUTHORIZATION tubedepth_owner: this connects with the migrator's
        # credential, and a schema it creates without this clause is owned by
        # the migrator rather than the owner role the bootstrap SQL uses in
        # production.
        connection.execute(text(f"CREATE SCHEMA {SCHEMA} AUTHORIZATION tubedepth_owner"))
        # SET ROLE again: create_all needs the owner's privileges on the
        # schema it just created, the same rule migrations/env.py follows,
        # rather than going through Database.create_schema() on a raw
        # migrator connection, which would fail with permission denied.
        connection.execute(text("SET ROLE tubedepth_owner"))
        Base.metadata.create_all(bind=connection)
        # A schema DROP ... CASCADE takes the GRANTs deploy/postgres-bootstrap.sql
        # made on the old schema object with it — a fresh schema, even of the
        # same name, is a new object with none of them. Without these, every
        # ordinary read or write this file does as tubedepth_runtime fails
        # with "permission denied", for a reason that has nothing to do with
        # what each test is actually checking.
        connection.execute(text(f"GRANT USAGE ON SCHEMA {SCHEMA} TO tubedepth_runtime"))
        connection.execute(
            text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {SCHEMA} "
                "TO tubedepth_runtime"
            )
        )
        connection.execute(
            text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {SCHEMA} TO tubedepth_runtime")
        )
    engine.dispose()

    database = Database(RUNTIME_URL or "")
    yield database


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
