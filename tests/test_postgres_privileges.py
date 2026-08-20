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

from tubedepth.database import Database
from tubedepth.errors import ConfigurationError

ROOT = Path(__file__).parent.parent
SCHEMA = "tubedepth"
BOOTSTRAP_SQL = ROOT / "deploy" / "postgres-bootstrap.sql"

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


def schema_scoped_grants() -> list[str]:
    """The GRANT / ALTER DEFAULT PRIVILEGES block between
    deploy/postgres-bootstrap.sql's SCHEMA-SCOPED-GRANTS markers, split into
    individual statements.

    Not retyped here on purpose. Those ACLs are tied to the schema's own OID,
    so `migrated_database` below has to re-apply them after every DROP SCHEMA
    ... CASCADE — and if this test file kept its own copy of that SQL, editing
    or deleting a line in the real bootstrap file (say, one of the
    ALTER DEFAULT PRIVILEGES lines rule 1 calls "not optional") would leave
    this suite green while production broke. Reading the block out of the
    file itself is what makes a deleted line here fail here too.
    """
    text_ = BOOTSTRAP_SQL.read_text()
    start = text_.index("-- SCHEMA-SCOPED-GRANTS-BEGIN")
    end = text_.index("-- SCHEMA-SCOPED-GRANTS-END")
    block = text_[start:end]
    statements = [line if not line.strip().startswith("--") else "" for line in block.splitlines()]
    return [stmt.strip() for stmt in "\n".join(statements).split(";") if stmt.strip()]


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
    # rather than DDL specifically denied. Read out of the bootstrap file
    # itself (see schema_scoped_grants) rather than retyped, so a deleted
    # ALTER DEFAULT PRIVILEGES line there is a deleted line here too.
    engine = create_engine(MIGRATOR_URL or "")
    with engine.begin() as connection:
        connection.execute(text("SET ROLE tubedepth_owner"))
        for statement in schema_scoped_grants():
            connection.execute(text(statement))
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


@needs_postgres
def test_the_runtime_and_migrator_roles_pin_their_session_timezone_to_utc(
    migrated_database: None,
) -> None:
    """Rule 9's other half, the one Task 4 found while proving `AT TIME ZONE
    'UTC'` in the timestamptz migration: a downgrade with no explicit `AT TIME
    ZONE` clause converts through the *session's* TimeZone, and nothing before
    this pinned that session setting to UTC. Same pg_db_role_setting trap as
    the timeout test above — pg_roles.rolconfig is empty for a per-database
    `ALTER ROLE ... IN DATABASE ... SET ...` even though it is in effect.
    """
    engine = create_engine(MIGRATOR_URL or "")
    with engine.connect() as connection:
        for role in ("tubedepth_runtime", "tubedepth_migrator"):
            settings = dict(
                item.split("=", 1)
                for item in connection.execute(
                    text("""
                    SELECT unnest(drs.setconfig)
                    FROM pg_db_role_setting drs
                    JOIN pg_roles r ON r.oid = drs.setrole
                    JOIN pg_database d ON d.oid = drs.setdatabase
                    WHERE r.rolname = :role AND d.datname = current_database()
                    """),
                    {"role": role},
                ).scalars()
            )
            assert settings.get("TimeZone") == "UTC", f"{role} does not pin TimeZone to UTC"
    engine.dispose()


@needs_postgres
def test_the_runtime_role_can_do_the_dml_it_is_granted(migrated_database: None) -> None:
    """The positive control the four negative tests above need.

    Deleting the `GRANT USAGE ON SCHEMA` statement from `schema_scoped_grants`
    (or from `deploy/postgres-bootstrap.sql`, since that is where it reads
    from) makes every one of those four tests pass for the wrong reason:
    `permission denied for schema tubedepth` refuses `CREATE TABLE` just as
    effectively as a working grant does, and the assertion there cannot tell
    the difference between "DDL is specifically denied" and "runtime has no
    access to anything at all". This test is what tells them apart — it fails
    if runtime cannot do the DML rule 1 says it must be able to.
    """
    engine = create_engine(RUNTIME_URL or "")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tubedepth.jobs "
                "(identifier, kind, target, refresh, state, attempt_count, "
                "max_attempts, scheduled_at, created_at, webhook_attempts) "
                "VALUES "
                "('positive-control', 'video.metadata', 'dQw4w9WgXcQ', false, "
                "'queued', 0, 3, now(), now(), 0)"
            )
        )
        count = connection.execute(
            text("SELECT count(*) FROM tubedepth.jobs WHERE identifier = 'positive-control'")
        ).scalar_one()
    engine.dispose()
    assert count == 1


@needs_postgres
def test_a_table_created_after_the_grants_is_reachable_through_default_privileges(
    migrated_database: None,
) -> None:
    """The proof rule 1's `ALTER DEFAULT PRIVILEGES` lines earn their keep.

    `migrated_database` re-applies `schema_scoped_grants()` — including the
    explicit `GRANT ... ON ALL TABLES` — before this test runs, and that
    explicit grant only covers tables that already existed at the moment it
    ran. It says nothing about a table created afterwards. If the three
    ALTER DEFAULT PRIVILEGES statements in deploy/postgres-bootstrap.sql were
    deleted, `schema_scoped_grants()` would read a shorter block, apply fewer
    statements, and this table — created by the owner after the fixture's
    grants ran, the same order a real migration follows in production — would
    be unreachable by runtime. That is exactly the failure rule 1 warns about:
    discovered at the first request after a deploy, not during it.
    """
    engine = create_engine(MIGRATOR_URL or "")
    with engine.begin() as connection:
        connection.execute(text("SET ROLE tubedepth_owner"))
        connection.execute(
            text("CREATE TABLE tubedepth.created_after_grants (id bigint PRIMARY KEY)")
        )
    engine.dispose()

    runtime_engine = create_engine(RUNTIME_URL or "")
    with runtime_engine.begin() as connection:
        connection.execute(text("INSERT INTO tubedepth.created_after_grants (id) VALUES (1)"))
        count = connection.execute(
            text("SELECT count(*) FROM tubedepth.created_after_grants")
        ).scalar_one()
    runtime_engine.dispose()
    assert count == 1


@needs_postgres
def test_verify_placement_accepts_the_search_path_the_bootstrap_sets() -> None:
    """Untouched, a runtime connection's `search_path` already leads with
    `tubedepth` — `deploy/postgres-bootstrap.sql`'s
    `ALTER ROLE ... SET search_path = tubedepth, pg_catalog` put it there,
    and that value carries a second entry (`pg_catalog`) after the schema
    name. `verify_placement` must accept this, or a correct deployment could
    never start. No migration is required first: the check has to hold
    before any table exists.
    """
    database = Database(RUNTIME_URL or "")
    database.verify_placement()


@needs_postgres
def test_verify_placement_refuses_a_search_path_that_does_not_lead_with_the_schema() -> None:
    """The failure mode #16 exists for: skip the bootstrap's `ALTER ROLE`
    line, or run against a host set up by hand without it, and the
    connection's `search_path` leads with `public` instead of `tubedepth`.
    Nothing about that fails on its own — tables would simply be created in
    the schema three other services share. This is the one query that turns
    it into a refusal instead.

    `connection.commit()` after the `SET` matters: without it, closing the
    connection rolls the plain (non-`LOCAL`) `SET` back before it can affect
    the next checkout from the pool, and this test would pass for the wrong
    reason — the connection `verify_placement` opens would see the
    role's own correct `search_path`, never the tampered one.
    """
    database = Database(RUNTIME_URL or "")
    with database._read_engine.connect() as connection:
        connection.execute(text("SET search_path TO public"))
        connection.commit()

    with pytest.raises(ConfigurationError, match="ALTER ROLE") as refusal:
        database.verify_placement()
    assert "public" in str(refusal.value)


@needs_postgres
def test_is_migrated_sees_the_schema_through_the_migrator_role_without_set_role(
    migrated_database: None,
) -> None:
    """`tubedepth migrate`, in a real deployment, runs against the migrator
    URL. `migrations/env.py` does `SET ROLE tubedepth_owner` for the DDL
    itself, but the post-migrate artifact check in `cli.migrate` reopens the
    database through `_database()` — a plain connection, no `SET ROLE` — and
    that used to call `is_migrated()`, which reflected `False` even though
    the schema it just built is right there: the migrator is `NOINHERIT`
    (rule 1) and has no direct `USAGE` on `tubedepth`, so an *unqualified*
    lookup resolves `current_schema()` to `pg_catalog` rather than skipping
    to `pg_catalog` and stopping, per PostgreSQL's rule of silently passing
    over a `search_path` entry the role cannot use. The result: a `✓ … is at
    the current schema` from the upgrade and a `✗ no schema at …` from the
    very next line, for the same run.

    First confirm the scenario this guards against is real, so a future
    bootstrap change that starts granting the migrator direct `USAGE` cannot
    make this test pass for the wrong reason (nothing left to see through):
    """
    engine = create_engine(MIGRATOR_URL or "")
    with engine.connect() as connection:
        current_schema = connection.exec_driver_sql("SELECT current_schema()").scalar_one()
    engine.dispose()
    assert current_schema != SCHEMA, (
        "the migrator role can already see tubedepth without SET ROLE — "
        "this test no longer exercises the regression it exists to guard"
    )

    database = Database(MIGRATOR_URL or "")
    assert database.is_migrated() is True


@needs_postgres
def test_the_migrate_command_says_one_true_thing_against_the_migrator_url(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The end-to-end regression: `tubedepth migrate`, run under the
    migrator credential — the one a real deployment uses for this command —
    against an already-migrated database. Before this fix, the post-migrate
    artifact-count check reopened the database through a plain `_database()`
    connection, no `SET ROLE`, and either misreported `is_migrated()` as
    `False` (`✓ … is at the current schema` immediately followed by a false
    `✗ no schema at …`) or, once that was fixed on its own, crashed with an
    unhandled `UndefinedTable` — the migrator is deployment-only (rule 1) and
    has no direct `SELECT` on `tubedepth`'s tables. `SET ROLE tubedepth_owner`
    for that query, the same privilege the upgrade itself already needed, is
    what makes the whole command say exactly one true thing.
    """
    from typer.testing import CliRunner

    from tubedepth.cli import application

    monkeypatch.setenv("TUBEDEPTH_DATABASE_URL", MIGRATOR_URL or "")
    runner = CliRunner()

    result = runner.invoke(application, ["migrate", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert result.output.count("is at the current schema") == 1
    assert "no schema at" not in result.output
