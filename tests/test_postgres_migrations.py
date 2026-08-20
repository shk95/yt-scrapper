"""The migrations, run against the database this project is moving to.

SQLite and PostgreSQL disagree about enough DDL that "the migrations work" is
not one fact. `docs/status.md` records the decision to move; this is the check
that keeps each revision honest about both dialects while both exist, and the
only check that would have caught the boolean default in `50ee31ae8b82` —
`ADD COLUMN refresh BOOLEAN DEFAULT 0` is required by SQLite and rejected by
PostgreSQL, which has no integer-to-boolean cast in a DEFAULT.

Marked `postgres` and deselected by default, because it needs a server the
offline suite must not assume. It is not a test nobody runs: `just postgres`
starts a container and runs it, and CI does the same through a service
container. `decisions/003` is about capabilities with no caller; this one has
two.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).parent.parent
SCHEMA = "tubedepth"

pytestmark = pytest.mark.postgres

URL = os.environ.get("TUBEDEPTH_TEST_POSTGRES_URL")
needs_postgres = pytest.mark.skipif(
    not URL, reason="set TUBEDEPTH_TEST_POSTGRES_URL, or run `just postgres`"
)


def alembic(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "TUBEDEPTH_DATABASE_URL": URL or "",
            "PYTHONPATH": str(ROOT / "src"),
        },
    )


@pytest.fixture
def empty_database() -> Iterator[None]:
    """A schema with nothing in it, before and after.

    Dropped rather than truncated: a migration that fails halfway leaves DDL
    behind on some dialects, and a test whose starting state depends on whether
    the previous one passed is a test that reports the wrong failure.
    """
    engine = create_engine(URL or "")
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    yield
    engine.dispose()


@needs_postgres
def test_every_revision_applies_to_postgresql(empty_database: None) -> None:
    """The whole chain, not just the initial schema.

    PostgreSQL runs DDL inside a transaction, so a chain that fails on the
    fourth revision rolls the first three back and leaves an empty database —
    the opposite of SQLite, where a partial upgrade is what produced the
    `duplicate column name` in `docs/troubleshooting.md`. That makes the
    failure clean and the assertion simple: either all of it is there or none.
    """
    result = alembic("upgrade", "head")

    assert result.returncode == 0, result.stderr


@needs_postgres
def test_the_migrated_schema_is_the_one_the_models_describe(empty_database: None) -> None:
    """Autogenerate against the migrated database must find nothing to do.

    The same property `test_migrations.py` asserts on SQLite, which cannot see
    a difference that only exists on PostgreSQL — a server default rendered per
    dialect, or a type the SQLite dialect collapses.

    Note what is *not* passed here. `docs/shared-postgres.md` reaches for
    `version_table_schema="tubedepth"`, and setting it against a connection
    whose `search_path` is already `tubedepth` makes autogenerate propose
    `drop_table('alembic_version')`: alembic excludes its own version table by
    comparing the configured schema against the reflected one, reflection under
    a `search_path` reports `None`, and `"tubedepth" != None`. The rule meant to
    prevent a spurious `drop_table` produces one. `search_path` alone already
    puts `alembic_version` in the service's schema — checked, it lands there —
    so the two settings are alternatives and naming both is the bug.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from tubedepth.models import Base

    assert alembic("upgrade", "head").returncode == 0

    engine = create_engine(URL or "")
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"include_schemas": False})
        difference = compare_metadata(context, Base.metadata)
    engine.dispose()

    assert difference == [], f"models and migrations disagree on postgresql: {difference}"


@needs_postgres
def test_the_migrations_go_back_down_again(empty_database: None) -> None:
    """Reversible on both dialects, or reversible on neither in practice —
    nobody checks which one the deployment is before running a downgrade."""
    assert alembic("upgrade", "head").returncode == 0

    result = alembic("downgrade", "base")

    assert result.returncode == 0, result.stderr
    engine = create_engine(URL or "")
    remaining = set(inspect(engine).get_table_names(schema=SCHEMA))
    engine.dispose()
    assert remaining <= {"alembic_version"}


@needs_postgres
def test_the_version_table_lands_in_the_services_own_schema(empty_database: None) -> None:
    """Rule 2 of `docs/shared-postgres.md`, asserted rather than assumed.

    The default is `public.alembic_version`, and on a shared database that is
    one row several services overwrite in turn — A's upgrade reading B's
    revision as head, re-running or skipping migrations, and the damage only
    visible once someone asks how far each service actually got. Here the
    role's `search_path` is what places it, so this is also the check that the
    role is set up the way the deployment assumes.
    """
    assert alembic("upgrade", "head").returncode == 0

    engine = create_engine(URL or "")
    with engine.connect() as connection:
        schemas = set(
            connection.execute(
                text("SELECT schemaname FROM pg_tables WHERE tablename = 'alembic_version'")
            ).scalars()
        )
    engine.dispose()

    assert schemas == {SCHEMA}, f"the version table is not the service's own: {schemas}"
