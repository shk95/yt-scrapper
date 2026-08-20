"""The boot path issues no DDL, which a shared database forbids.

`_database()` is what every CLI entry point goes through, and it used to call
`create_schema()` — so every `work`, `serve`, `enqueue`, `jobs` and `prune`
issued DDL against the database before doing anything else. That was a
convenience while this owned a file. On a database the other scrapers live in
it is rule 6 of `docs/shared-postgres.md`, and it breaks migrations here: a
boot that adds a column leaves `alembic_version` untouched, so the next
`alembic upgrade` tries to add a column that is already there. That is the
`duplicate column name` in `docs/troubleshooting.md`.

The two CLI tests below need the real `tubedepth` schema, not a throwaway
per-test one: `tubedepth migrate` runs migrations, and `migrations/env.py`'s
`SET ROLE tubedepth_owner` only has CREATE on the schema `deploy/postgres-
bootstrap.sql` actually granted it ownership of. Marked `postgres` for that
reason (Task 8 — since the cutover there is no SQLite fallback left for a CLI
test to fall back to instead).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from typer.testing import CliRunner

from tubedepth.cli import application
from tubedepth.database import Database

runner = CliRunner()

DDL_LEADERS = ("create", "alter", "drop", "truncate")
SCHEMA = "tubedepth"

pytestmark = pytest.mark.postgres

URL = os.environ.get("TUBEDEPTH_TEST_POSTGRES_URL")
RUNTIME_URL = os.environ.get("TUBEDEPTH_TEST_POSTGRES_RUNTIME_URL")
needs_postgres = pytest.mark.skipif(
    not URL or not RUNTIME_URL,
    reason="set TUBEDEPTH_TEST_POSTGRES_URL and _RUNTIME_URL, or run `just postgres`",
)

ROOT = Path(__file__).parent.parent
BOOTSTRAP_SQL = ROOT / "deploy" / "postgres-bootstrap.sql"


def _schema_scoped_grants() -> list[str]:
    """The GRANT / ALTER DEFAULT PRIVILEGES block between
    `deploy/postgres-bootstrap.sql`'s SCHEMA-SCOPED-GRANTS markers, split into
    individual statements — the same shape `test_postgres_privileges.py`'s
    `schema_scoped_grants` reads for the same reason: a `DROP SCHEMA ...
    CASCADE` takes the schema's ACL entries with it, and re-typing the block
    here would let a changed grant in the real bootstrap file go unnoticed by
    this one.
    """
    text_ = BOOTSTRAP_SQL.read_text()
    start = text_.index("-- SCHEMA-SCOPED-GRANTS-BEGIN")
    end = text_.index("-- SCHEMA-SCOPED-GRANTS-END")
    block = text_[start:end]
    statements = [line if not line.strip().startswith("--") else "" for line in block.splitlines()]
    return [stmt.strip() for stmt in "\n".join(statements).split(";") if stmt.strip()]


@pytest.fixture
def empty_database() -> Iterator[None]:
    """The real `tubedepth` schema, dropped and rebuilt — see
    `tests/test_postgres_migrations.py`'s fixture of the same name for why
    a failure partway through one test must not decide what the next starts
    from, and why `AUTHORIZATION tubedepth_owner` matters.

    Does not set `TUBEDEPTH_DATABASE_URL` itself: `tubedepth migrate` and the
    ordinary commands after it are different roles in production
    (`migrations/env.py`'s `SET ROLE tubedepth_owner` versus `tubedepth_runtime`'s
    plain DML), and the two tests below switch the variable between the two
    calls the same way a real deployment's two unit files would.

    Re-grants runtime's schema-scoped privileges after recreating the schema
    (`_schema_scoped_grants`, read out of `deploy/postgres-bootstrap.sql` the
    same way `test_postgres_privileges.py`'s helper of the same shape does,
    not retyped, so a changed grant there fails here too): a
    `DROP SCHEMA ... CASCADE` takes the previous ACL entries with it, and
    without the `ALTER DEFAULT PRIVILEGES` half in particular, `jobs` —
    created afterwards, by `migrate` — would be unreachable by
    `tubedepth_runtime` even though it is granted DML on the schema in
    general.
    """
    engine = create_engine(URL or "")
    with engine.begin() as connection:
        connection.execute(text("SET ROLE tubedepth_owner"))
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.execute(text("RESET ROLE"))
        connection.execute(text(f"CREATE SCHEMA {SCHEMA} AUTHORIZATION tubedepth_owner"))
        connection.execute(text("SET ROLE tubedepth_owner"))
        for statement in _schema_scoped_grants():
            connection.execute(text(statement))
    engine.dispose()

    yield
    engine = create_engine(URL or "")
    with engine.begin() as connection:
        connection.execute(text("SET ROLE tubedepth_owner"))
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
    engine.dispose()


@pytest.fixture
def recorded_ddl(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every DDL statement any engine emits, in order.

    Hooked at `before_cursor_execute` on the Engine class rather than on one
    instance, because the point is that *no* engine the boot path creates
    issues DDL — an assertion scoped to one instance would pass while a second
    engine did it.
    """
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.strip().lower().startswith(DDL_LEADERS):
            statements.append(statement.strip())

    from sqlalchemy.engine import Engine

    event.listen(Engine, "before_cursor_execute", record)
    yield statements
    event.remove(Engine, "before_cursor_execute", record)


@needs_postgres
def test_a_migrated_database_gets_no_ddl_when_a_command_opens_it(
    tmp_path: Path, empty_database: None, recorded_ddl: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TUBEDEPTH_DATABASE_URL", URL or "")
    assert runner.invoke(application, ["migrate", "--data-dir", str(tmp_path)]).exit_code == 0
    recorded_ddl.clear()

    # As `tubedepth_runtime`, the way `tubedepth work`/`serve` actually
    # connect — the migrator has no DML grant on `tubedepth`'s tables outside
    # `migrations/env.py`'s own `SET ROLE` (rule 1), so this is also what
    # makes the query below resolve `jobs` through `search_path` at all.
    monkeypatch.setenv("TUBEDEPTH_DATABASE_URL", RUNTIME_URL or "")
    result = runner.invoke(application, ["jobs", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert recorded_ddl == [], f"the boot path issued DDL: {recorded_ddl}"


@needs_postgres
def test_a_fresh_schema_is_refused_rather_than_given_ddl_or_a_traceback(
    tmp_path: Path, empty_database: None, recorded_ddl: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of #14: no schema is not a schema this silently builds.

    `_database()` used to answer a database with no schema by calling
    `create_schema()` — DDL on the boot path, the thing this whole file is
    about. Simply removing that call turns the same case into an unhandled
    `sqlalchemy.exc.ProgrammingError` (PostgreSQL's "relation does not exist")
    and a Rich traceback the first time a query runs, which is worse: this
    repository's boundary never prints a traceback (`test_cli.py`'s
    error-boundary test), and nothing tells the operator what to run. So the
    boot path must refuse cleanly instead, and still issue no DDL while
    deciding that.
    """
    monkeypatch.setenv("TUBEDEPTH_DATABASE_URL", RUNTIME_URL or "")
    result = runner.invoke(application, ["jobs", "--data-dir", str(tmp_path)])

    assert result.exit_code != 0
    assert "tubedepth migrate" in str(result.exception), result.output
    assert recorded_ddl == [], f"the refusal path issued DDL: {recorded_ddl}"


def test_create_schema_does_not_alter_a_table_that_has_fallen_behind(
    database: Database, database_url_for_tests: str
) -> None:
    """The repair is gone. `create_schema` creates what is missing and stops.

    It kept a real problem closed while there was no migration tool. There is
    one now — five revisions, and `tests/test_postgres_migrations.py` asserts
    that migrate-from-nothing and create-from-models agree — so the repair's
    whole job is covered by the thing that replaced it, and keeping it means a
    boot that silently disagrees with `alembic_version`.

    Uses the ordinary per-test `database` fixture rather than the real
    `tubedepth` schema: the property under test — `create_schema` does not
    repair a table that has fallen behind — does not depend on which schema
    it runs against, only on `Database.create_schema` itself.
    """
    from sqlalchemy import inspect

    with create_engine(database_url_for_tests).begin() as connection:
        connection.execute(text("ALTER TABLE jobs DROP COLUMN api_key_id"))

    database.create_schema()

    columns = {
        c["name"] for c in inspect(create_engine(database_url_for_tests)).get_columns("jobs")
    }
    assert "api_key_id" not in columns, "create_schema repaired a table instead of leaving it"
