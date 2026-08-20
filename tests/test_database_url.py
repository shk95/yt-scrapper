"""The database is named by a URL, because a shared server has no file path.

`Database` took a `Path` and formatted `sqlite+pysqlite:///` around it twice,
so `TUBEDEPTH_DATABASE_URL` was honoured by Alembic and ignored by the
application — the two could migrate and run against different databases with
nothing saying so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tubedepth.database import Database
from tubedepth.settings import database_url


def test_a_url_with_no_environment_names_the_sqlite_file_under_the_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TUBEDEPTH_DATABASE_URL", raising=False)

    assert database_url(tmp_path) == f"sqlite+pysqlite:///{tmp_path / 'tubedepth.db'}"


def test_the_environment_wins_over_the_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TUBEDEPTH_DATA_DIR keeps meaning the payload store after the cutover.

    A deployment on the shared instance has a database URL and a payload
    directory, and they are not the same thing — so naming a directory must
    never be able to redirect the database.
    """
    monkeypatch.setenv("TUBEDEPTH_DATABASE_URL", "postgresql+psycopg://u:p@h:5432/fleet")

    assert database_url(tmp_path) == "postgresql+psycopg://u:p@h:5432/fleet"


def test_the_dialect_is_readable_without_opening_a_connection(tmp_path: Path) -> None:
    """Task 5's startup guard and the dialect-conditional hooks both ask this."""
    assert Database(f"sqlite+pysqlite:///{tmp_path / 'x.db'}").dialect == "sqlite"
    assert Database("postgresql+psycopg://u:p@h:5432/fleet").dialect == "postgresql"


def test_a_postgresql_url_does_not_get_sqlite_pragmas(tmp_path: Path) -> None:
    """Constructing against PostgreSQL must not register SQLite's hooks.

    `create_engine` is lazy — no connection is opened here — so this asserts
    the wiring, which is the part that would otherwise fail at the first
    request with a syntax error inside an event handler.
    """
    database = Database("postgresql+psycopg://u:p@h:5432/fleet")

    assert database.sqlite_hooks_installed is False
