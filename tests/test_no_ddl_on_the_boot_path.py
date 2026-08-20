"""The boot path issues no DDL, which a shared database forbids.

`_database()` is what every CLI entry point goes through, and it used to call
`create_schema()` — so every `work`, `serve`, `enqueue`, `jobs` and `prune`
issued DDL against the database before doing anything else. That was a
convenience while this owned a file. On a database the other scrapers live in
it is rule 6 of `docs/shared-postgres.md`, and it breaks migrations here: a
boot that adds a column leaves `alembic_version` untouched, so the next
`alembic upgrade` tries to add a column that is already there. That is the
`duplicate column name` in `docs/troubleshooting.md`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import event
from typer.testing import CliRunner

from tubedepth.cli import application
from tubedepth.database import Database

runner = CliRunner()

DDL_LEADERS = ("create", "alter", "drop", "truncate")


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


def test_a_migrated_database_gets_no_ddl_when_a_command_opens_it(
    tmp_path: Path, recorded_ddl: list[str]
) -> None:
    assert runner.invoke(application, ["migrate", "--data-dir", str(tmp_path)]).exit_code == 0
    recorded_ddl.clear()

    result = runner.invoke(application, ["jobs", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert recorded_ddl == [], f"the boot path issued DDL: {recorded_ddl}"


def test_create_schema_does_not_alter_a_table_that_has_fallen_behind(tmp_path: Path) -> None:
    """The repair is gone. `create_schema` creates what is missing and stops.

    It kept a real problem closed while there was no migration tool. There is
    one now — five revisions, and `test_migrations.py` asserts that
    migrate-from-nothing and create-from-models agree — so the repair's whole
    job is covered by the thing that replaced it, and keeping it means a boot
    that silently disagrees with `alembic_version`.
    """
    from sqlalchemy import create_engine, inspect, text

    url = f"sqlite+pysqlite:///{tmp_path / 'tubedepth.db'}"
    Database(tmp_path / "tubedepth.db").create_schema()
    with create_engine(url).begin() as connection:
        connection.execute(text("ALTER TABLE jobs DROP COLUMN api_key_id"))

    Database(tmp_path / "tubedepth.db").create_schema()

    columns = {c["name"] for c in inspect(create_engine(url)).get_columns("jobs")}
    assert "api_key_id" not in columns, "create_schema repaired a table instead of leaving it"
