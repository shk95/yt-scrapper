"""Real migrations, and the properties that make this migration set trustworthy.

`Database.create_schema` only creates the tables the models describe (#14) —
it used to also repair a file that predates a column or index, but that
closed a real gap only while there was no migration tool, and kept the gap
open once there was one: a repaired file and `alembic_version` could quietly
disagree. This migration set is what covers that gap now, so what matters is
that it is trustworthy rather than merely present.

These check exactly that — that it produces the schema the models describe,
that it is reversible, and that nobody has left two heads for the next person
to discover mid-deploy.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).parent.parent


def alembic(*arguments: str, database: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "TUBEDEPTH_DATABASE_URL": f"sqlite+pysqlite:///{database}",
            "PYTHONPATH": str(ROOT / "src"),
        },
    )


def test_the_migrations_have_exactly_one_head(tmp_path: Path) -> None:
    """Two heads is a merge nobody noticed, and it fails at deploy time on the
    machine least able to fix it."""
    result = alembic("heads", database=tmp_path / "t.db")

    assert result.returncode == 0, result.stderr
    assert len([line for line in result.stdout.splitlines() if line.strip()]) == 1, result.stdout


def test_upgrading_an_empty_database_produces_the_schema_the_models_describe(
    tmp_path: Path,
) -> None:
    """The property that matters: migrate-from-nothing and create-from-models
    must agree. When they drift, one half of the deployments is running a
    schema nobody tested against."""
    from tubedepth.database import Database
    from tubedepth.models import Base

    migrated = tmp_path / "migrated.db"
    result = alembic("upgrade", "head", database=migrated)
    assert result.returncode == 0, result.stderr

    Database(f"sqlite+pysqlite:///{tmp_path / 'created.db'}").create_schema()

    def schema(path: Path) -> dict[str, set[str]]:
        inspector = inspect(create_engine(f"sqlite+pysqlite:///{path}"))
        return {
            table: {column["name"] for column in inspector.get_columns(table)}
            for table in inspector.get_table_names()
            if table != "alembic_version"
        }

    assert schema(migrated) == schema(tmp_path / "created.db")
    assert set(schema(migrated)) == set(Base.metadata.tables)


def test_the_migrations_go_back_down_again(tmp_path: Path) -> None:
    """A migration that cannot be reversed is one nobody dares deploy on a
    Friday. Cheap to keep true while there is one revision; expensive to
    retrofit after ten."""
    database = tmp_path / "t.db"
    assert alembic("upgrade", "head", database=database).returncode == 0

    result = alembic("downgrade", "base", database=database)

    assert result.returncode == 0, result.stderr
    inspector = inspect(create_engine(f"sqlite+pysqlite:///{database}"))
    assert set(inspector.get_table_names()) <= {"alembic_version"}


def test_the_models_and_the_migrations_have_not_drifted(tmp_path: Path) -> None:
    """The check that keeps this honest as the schema changes.

    Autogenerate against a migrated database must find nothing to do. If it
    finds something, a model was changed without a migration — which works
    perfectly on every developer machine, because `create_schema` builds from
    the models, and fails on the one deployment that migrates.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    database = tmp_path / "t.db"
    assert alembic("upgrade", "head", database=database).returncode == 0

    from tubedepth.models import Base

    engine = create_engine(f"sqlite+pysqlite:///{database}")
    with engine.connect() as connection:
        difference = compare_metadata(MigrationContext.configure(connection), Base.metadata)

    assert difference == [], f"models and migrations disagree: {difference}"


def test_the_cli_can_stamp_a_database_that_predates_migrations(
    tmp_path: Path, database_url_for_tests: str
) -> None:
    """The one-time problem every project gets exactly once.

    This database existed for a day before migrations did. Upgrading it would
    try to create tables that are already there; the honest move is to record
    which revision its schema already matches and migrate forward from then on.
    """
    from typer.testing import CliRunner

    from tubedepth.cli import application
    from tubedepth.database import Database

    Database(database_url_for_tests).create_schema()

    result = CliRunner().invoke(application, ["migrate", "--stamp", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'tubedepth.db'}")
    with engine.connect() as connection:
        from sqlalchemy import text

        stamped = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert stamped, "nothing was recorded, so the next upgrade will try to create what exists"


def test_the_cli_upgrades_an_empty_database(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from tubedepth.cli import application

    result = CliRunner().invoke(application, ["migrate", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    inspector = inspect(create_engine(f"sqlite+pysqlite:///{tmp_path / 'tubedepth.db'}"))
    assert "jobs" in inspector.get_table_names()
