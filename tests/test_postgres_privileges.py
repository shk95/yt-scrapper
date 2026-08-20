"""The three-role separation, asserted against a real PostgreSQL server.

`docs/shared-postgres.md` rule 1 exists because a runtime credential that owns
its own schema turns a bad `DELETE` into a possible `DROP`: the same session
that runs the application's DML can also alter or drop the tables it is
supposed to be confined to. `deploy/postgres-bootstrap.sql` now creates three
roles instead of one — `tubedepth_owner` (schema owner, `NOLOGIN`),
`tubedepth_migrator` (deployment only), and `tubedepth_runtime` (the one the
application logs in as) — and this file is the negative-space proof that the
separation actually holds: the runtime role can read and write, and nothing
else.

Marked `postgres` and deselected by default, for the same reason
`test_postgres_migrations.py` is: it needs a server the offline suite must not
assume. `just postgres` starts one and runs this, connecting as
`tubedepth_runtime` rather than the migrator — the privilege boundary under
test is the runtime role's, not the one that ran the migrations.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

ROOT = Path(__file__).parent.parent
SCHEMA = "tubedepth"

pytestmark = pytest.mark.postgres

MIGRATOR_URL = os.environ.get("TUBEDEPTH_TEST_POSTGRES_URL")
RUNTIME_URL = os.environ.get("TUBEDEPTH_TEST_POSTGRES_RUNTIME_URL")
needs_postgres = pytest.mark.skipif(
    not RUNTIME_URL,
    reason="set TUBEDEPTH_TEST_POSTGRES_RUNTIME_URL, or run `just postgres`",
)


def alembic(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "TUBEDEPTH_DATABASE_URL": MIGRATOR_URL or "",
            "PYTHONPATH": str(ROOT / "src"),
        },
    )


@pytest.fixture
def migrated_database() -> Iterator[None]:
    """A schema at `head`, dropped and rebuilt for the same reason
    `test_postgres_migrations.py`'s `empty_database` is: a failure partway
    through one test must not decide what the next one starts from.

    The privilege tests need real objects — `tubedepth.jobs` in particular —
    to try (and fail) to alter, so this migrates rather than just creating an
    empty schema.
    """
    engine = create_engine(MIGRATOR_URL or "")
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
        connection.execute(text(f"CREATE SCHEMA {SCHEMA} AUTHORIZATION tubedepth_owner"))
    engine.dispose()

    result = alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr

    # A schema DROP ... CASCADE takes the GRANTs deploy/postgres-bootstrap.sql
    # made on the old schema object with it — a fresh schema, even of the same
    # name, is a new object with none of them. Without re-applying these here,
    # tubedepth_runtime would have no DML access at all, and every negative
    # test in this module would pass for the wrong reason: nothing granted,
    # rather than DDL specifically denied.
    engine = create_engine(MIGRATOR_URL or "")
    with engine.begin() as connection:
        connection.execute(text("SET ROLE tubedepth_owner"))
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

    yield


@needs_postgres
@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE tubedepth.must_fail (id bigint)",
        "ALTER TABLE tubedepth.jobs ADD COLUMN must_fail text",
        "DROP TABLE tubedepth.jobs",
        "TRUNCATE tubedepth.jobs",
    ],
)
def test_the_runtime_role_cannot_change_the_schema(statement: str, migrated_database: None) -> None:
    engine = create_engine(RUNTIME_URL or "")
    with pytest.raises(ProgrammingError) as refused, engine.begin() as connection:
        connection.execute(text(statement))
    # PostgreSQL phrases this two ways, and which one runtime gets depends on
    # the statement, not on whether it was refused: CREATE and TRUNCATE fail a
    # privilege check ("permission denied for ..."), but ALTER and DROP are
    # owner-only operations PostgreSQL checks by ownership directly ("must be
    # owner of table ..."). Both are InsufficientPrivilege; only the message
    # differs, so the assertion accepts either rather than asserting the one
    # phrasing the regulation happens to show as its example.
    message = str(refused.value).lower()
    assert "permission denied" in message or "must be owner of" in message, message
    engine.dispose()


@needs_postgres
def test_every_object_in_the_schema_is_owned_by_the_owner_role(
    migrated_database: None,
) -> None:
    engine = create_engine(MIGRATOR_URL or "")
    with engine.connect() as connection:
        rows = connection.execute(
            text("""
            SELECT n.nspname, c.relname, pg_get_userbyid(c.relowner) AS owner
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'tubedepth'
              AND pg_get_userbyid(c.relowner) <> 'tubedepth_owner'
            """)
        ).all()
    engine.dispose()
    assert rows == [], f"objects not owned by tubedepth_owner: {rows}"


@needs_postgres
def test_the_runtime_role_carries_the_timeouts_the_regulation_requires(
    migrated_database: None,
) -> None:
    # Not pg_roles.rolconfig: the bootstrap SQL uses
    # `ALTER ROLE ... IN DATABASE ... SET ...`, which is per-(role, database)
    # and stored in pg_db_role_setting, not the role-wide rolconfig the
    # regulation's own audit query names. rolconfig is empty here even though
    # the settings are very much in effect on connect — this query is what
    # actually finds them.
    engine = create_engine(MIGRATOR_URL or "")
    with engine.connect() as connection:
        settings = dict(
            item.split("=", 1)
            for item in connection.execute(
                text("""
                SELECT unnest(drs.setconfig)
                FROM pg_db_role_setting drs
                JOIN pg_roles r ON r.oid = drs.setrole
                JOIN pg_database d ON d.oid = drs.setdatabase
                WHERE r.rolname = 'tubedepth_runtime' AND d.datname = current_database()
                """)
            ).scalars()
        )
    engine.dispose()
    for name in ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout"):
        assert name in settings, f"{name} is not set on the runtime role"
