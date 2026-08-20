"""The database is named by a URL, because a shared server has no file path.

`Database` took a `Path` and formatted `sqlite+pysqlite:///` around it twice,
so `TUBEDEPTH_DATABASE_URL` was honoured by Alembic and ignored by the
application — the two could migrate and run against different databases with
nothing saying so. Since the cutover (#15) there is a second property to
protect here too: `settings.database_url` has no SQLite fallback any more,
and `Database` refuses any URL that is not PostgreSQL — except the one the
transfer tool explicitly asks to accept, which is why a couple of tests below
still construct a SQLite URL on purpose rather than by leftover habit.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tubedepth.database import Database
from tubedepth.errors import ConfigurationError
from tubedepth.settings import database_url

ROOT = Path(__file__).parent.parent


def test_an_unset_url_is_refused_rather_than_given_a_sqlite_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback this module used to test for is gone (#15): an operator
    who forgot to set `TUBEDEPTH_DATABASE_URL` gets a named refusal instead of
    a SQLite file quietly opened in its place."""
    monkeypatch.delenv("TUBEDEPTH_DATABASE_URL", raising=False)

    with pytest.raises(ConfigurationError, match="TUBEDEPTH_DATABASE_URL"):
        database_url()


def test_the_environment_is_the_only_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TUBEDEPTH_DATABASE_URL", "postgresql+psycopg://u:p@h:5432/fleet")

    assert database_url() == "postgresql+psycopg://u:p@h:5432/fleet"


def test_the_dialect_is_readable_without_opening_a_connection(tmp_path: Path) -> None:
    """`Database.dialect` is asked by `verify_placement()` and `is_migrated()`
    on every URL that reaches them, including the SQLite source
    `tubedepth transfer --from` still accepts — so both dialects have to
    report correctly, not only the one the application itself runs on.

    `create_engine` is lazy — no connection is opened here — so this also
    asserts the wiring at construction time, which is the part that would
    otherwise fail at the first request with a syntax error inside an event
    handler installed for the wrong dialect.
    """
    sqlite = Database(f"sqlite+pysqlite:///{tmp_path / 'x.db'}", allow_sqlite_source=True)
    postgres = Database("postgresql+psycopg://u:p@h:5432/fleet")

    assert sqlite.dialect == "sqlite"
    assert postgres.dialect == "postgresql"


def test_the_write_engine_pool_is_sized_off_worker_concurrency(tmp_path: Path) -> None:
    """`cli.work` passes `pool_size=max_overflow=concurrency` so the write
    engine's ceiling actually covers `Worker.drain`'s claim and lease-renewal
    threads, instead of the fixed default meant for the API. The read engine
    must stay at the default regardless — `Worker.reap` is its only caller
    and never wants more than one session at a time, so scaling it too would
    only spend more of the shared budget for no benefit.

    Construction only, same as `test_the_dialect_is_readable_without_opening_a_connection`
    above — `create_engine` is lazy, so this proves the wiring without a
    server.
    """
    default = Database("postgresql+psycopg://u:p@h:5432/fleet")
    assert default._engine.pool.size() == 2  # type: ignore[attr-defined]  # noqa: SLF001
    assert default._engine.pool._max_overflow == 2  # type: ignore[attr-defined]  # noqa: SLF001

    scaled = Database("postgresql+psycopg://u:p@h:5432/fleet", pool_size=8, max_overflow=8)
    assert scaled._engine.pool.size() == 8  # type: ignore[attr-defined]  # noqa: SLF001
    assert scaled._engine.pool._max_overflow == 8  # type: ignore[attr-defined]  # noqa: SLF001
    assert scaled._read_engine.pool.size() == 2  # type: ignore[attr-defined]  # noqa: SLF001
    assert scaled._read_engine.pool._max_overflow == 2  # type: ignore[attr-defined]  # noqa: SLF001


def test_a_non_postgresql_url_is_refused(tmp_path: Path) -> None:
    """The cutover's own guarantee (#15): every ordinary caller of `Database`
    gets a named refusal rather than quietly running against SQLite again."""
    with pytest.raises(ConfigurationError, match="PostgreSQL only"):
        Database(f"sqlite+pysqlite:///{tmp_path / 'x.db'}")


def test_transfer_source_is_the_one_place_sqlite_is_still_accepted(tmp_path: Path) -> None:
    """`allow_sqlite_source=True` is what `tubedepth transfer --from` passes.
    Without an explicit opt-in, the refusal above applies to it exactly as it
    does to everything else."""
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'x.db'}", allow_sqlite_source=True)

    assert database.dialect == "sqlite"


def test_verify_placement_is_a_no_op_on_a_sqlite_transfer_source(tmp_path: Path) -> None:
    """SQLite has no `search_path` to get wrong, and no server-side role to
    misconfigure — the whole failure mode `verify_placement` guards against
    only exists on PostgreSQL. It must return without even opening a
    connection, or a legitimate transfer source would be refused for a
    condition that cannot occur there. `transfer()` calls this on its source
    (see `tubedepth.transfer`), so it still has to hold post-cutover.
    """
    Database(
        f"sqlite+pysqlite:///{tmp_path / 'placement.db'}", allow_sqlite_source=True
    ).verify_placement()


def test_an_ambient_database_url_does_not_redirect_the_suite(tmp_path: Path) -> None:
    """The regression this module exists to close, reproduced directly.

    `_database()` honours `TUBEDEPTH_DATABASE_URL` — that is the point of the
    cutover — which means a value already sitting in an operator's shell
    (naming the shared fleet PostgreSQL, say) used to be inert to this suite
    and is not any more. Without `refuse_an_ambient_database_url` in
    `conftest.py`, running the CLI tests with the variable set redirects every
    `_database()` call at that path; on a real operator shell the same gap
    would run `tubedepth migrate` and then DML against the fleet database.

    A subprocess, not a monkeypatch in this process: the guard runs once per
    test as fixture setup, before the test body executes, so a value this test
    set itself would simply be honoured by anything it calls afterwards and
    would prove nothing about a value that was already there when pytest
    started — which is the actual shape of the bug. The ambient value points
    at a SQLite file specifically, because that is what would have been
    silently accepted before the cutover; today the guard has to intercept it
    before `Database` even gets a chance to refuse it for being SQLite, since
    a refusal for the wrong reason would also make this test pass.
    """
    ambient = tmp_path / "ambient.db"
    env = dict(os.environ)
    env["TUBEDEPTH_DATABASE_URL"] = f"sqlite+pysqlite:///{ambient}"

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_cli.py", "-q"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not ambient.exists(), (
        f"the ambient TUBEDEPTH_DATABASE_URL redirected the suite: {result.stdout}"
    )
