"""The command line's own behaviour, as opposed to the services beneath it.

Only the parts that are the CLI's job: argument parsing, and refusing input it
cannot honour. Anything that would reach YouTube belongs in the live contracts.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

from tubedepth.cli import application
from tubedepth.database import Database
from tubedepth.errors import ConfigurationError
from tubedepth.models import Artifact, Job, WorkerControl
from tubedepth.settings import database_url

runner = CliRunner()

SCHEMA = "tubedepth"
POSTGRES_URL = os.environ.get("TUBEDEPTH_TEST_POSTGRES_URL")


@pytest.fixture(autouse=True)
def _cli_database_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point every command in this file at a real, isolated database.

    `--data-dir`'s SQLite fallback is gone (#15, Task 8): `settings.database_url`
    now raises unless `TUBEDEPTH_DATABASE_URL` is set, so a CLI invocation with
    no other setup has nothing to open. Nearly every test below relied on that
    fallback for its own private SQLite file; autouse turns replacing it into
    one fixture instead of thirty edits.

    The literal `tubedepth` schema, not a throwaway per-test one
    (`database_url_for_tests`, used elsewhere in this suite): `verify_placement()`
    — which every `_database()` call goes through — checks the connection's
    `search_path` against `Database.SCHEMA`, a fixed `"tubedepth"`, by design
    (it is what proves a deployment's `ALTER ROLE ... SET search_path` actually
    took effect). A per-test schema would fail that check for a reason that has
    nothing to do with what any test here is exercising, so this file drops and
    rebuilds the one schema instead, sequentially, the same shape
    `tests/test_postgres_migrations.py`'s `empty_database` fixture uses.

    Connected as `tubedepth_migrator` throughout, with two harness-only grants
    beyond what `deploy/postgres-bootstrap.sql` gives that role in production:
    `CREATE` (most tests here call `Database.create_schema()` directly rather
    than running a real migration, for speed) and the same DML
    `tubedepth_runtime` gets (this file's commands read and write rows, and
    production never sends that traffic in as the migrator — but a single
    `TUBEDEPTH_DATABASE_URL` has to stand in for both roles across one test
    file, or every test would need two credentials for two different halves of
    one command). Real deployments never grant either. `tool/checks/test`
    already does the same kind of test-only grant for a different reason
    (`GRANT CREATE ON DATABASE fleet TO tubedepth_migrator`, harness setup).
    """
    if not POSTGRES_URL:
        pytest.skip("set TUBEDEPTH_TEST_POSTGRES_URL, or run `just postgres`")

    engine = create_engine(POSTGRES_URL)
    with engine.begin() as connection:
        connection.execute(text("SET ROLE tubedepth_owner"))
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.execute(text("RESET ROLE"))
        connection.execute(text(f"CREATE SCHEMA {SCHEMA} AUTHORIZATION tubedepth_owner"))
        connection.execute(text("SET ROLE tubedepth_owner"))
        connection.execute(text(f"GRANT USAGE, CREATE ON SCHEMA {SCHEMA} TO tubedepth_migrator"))
        connection.execute(
            text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {SCHEMA} "
                "TO tubedepth_migrator"
            )
        )
        connection.execute(
            text(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO tubedepth_migrator"
            )
        )
    engine.dispose()

    monkeypatch.setenv("TUBEDEPTH_DATABASE_URL", POSTGRES_URL)
    yield

    engine = create_engine(POSTGRES_URL)
    with engine.begin() as connection:
        connection.execute(text("SET ROLE tubedepth_owner"))
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
    engine.dispose()


def _database(data_directory: Path) -> Database:
    """The database a CLI invocation opens — `data_directory` no longer names
    it (there is no SQLite fallback under `--data-dir` to name any more), only
    `TUBEDEPTH_DATABASE_URL` does, which `_cli_database_url` above points at
    the real `tubedepth` schema, dropped and rebuilt before every test in this
    file runs. The parameter stays so every call site here (many of them
    predating the cutover) keeps reading the way it always did: "the database
    this data directory's commands use."
    """
    return Database(database_url())


def queued_targets(data_directory: Path) -> list[str]:
    """Targets in the queue, and an empty list when there is no queue at all.

    A command that refuses its arguments should not have created a schema, so
    "relation does not exist" is the same answer as "no rows" for these tests.
    `DBAPIError` rather than `ProgrammingError` specifically: PostgreSQL raises
    the former for a missing table, but the shape of "no schema behind this
    connection at all" is worth tolerating too rather than pinning to today's
    exact exception class.
    """
    try:
        with _database(data_directory).session(readonly=True) as session:
            return list(session.scalars(select(Job.target)))
    except DBAPIError:
        return []


def migrated(data_directory: Path) -> Path:
    """A data directory whose database already has the schema.

    `_database()` no longer creates one on the boot path (#14) — that is now
    `tubedepth migrate`'s job alone — so a test that exercises a command
    needing tables to already exist has to bring them into being itself.
    """
    data_directory.mkdir(parents=True, exist_ok=True)
    _database(data_directory).create_schema()
    return data_directory


def test_a_video_id_beginning_with_a_dash_is_a_target_and_not_an_option(
    tmp_path: Path,
) -> None:
    """YouTube ids are base64url, so a leading `-` is ordinary.

    One in a hundred of them starts with one, which is often enough to break a
    real sweep and rare enough that nobody hits it while trying things out.
    Click reads it as an option and reports `No such option: -2`, which names
    neither the id nor the video.
    """
    result = runner.invoke(
        application,
        ["enqueue", "video.transcript", "-2BFZsiVejU", "--data-dir", str(migrated(tmp_path))],
    )

    assert result.exit_code == 0, result.output
    assert queued_targets(tmp_path) == ["-2BFZsiVejU"]


def test_a_mistyped_option_is_refused_rather_than_queued_as_a_target(
    tmp_path: Path,
) -> None:
    """The cost of accepting leading dashes, paid back deliberately.

    Whatever makes `-2BFZsiVejU` a target would also make `--thn metadata` two
    targets, and a hundred jobs that can only fail is worse than an error. A
    double dash is never a video id, so it can be refused by shape.
    """
    result = runner.invoke(
        application,
        [
            "enqueue",
            "video.transcript",
            "dQw4w9WgXcQ",
            "--thn",
            "video.metadata",
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    assert "--thn" in str(result.exception), result.output
    assert queued_targets(tmp_path) == []


def test_the_error_boundary_prints_a_refusal_instead_of_a_traceback(
    tmp_path: Path,
) -> None:
    """`main` is the boundary, not the typer app, so it is what gets tested."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tubedepth.cli",
            "enqueue",
            "video.transcript",
            "--thn",
            "video.metadata",
            "--data-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "✗ unknown option: --thn" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_enqueue_records_a_refresh_so_the_worker_bypasses_the_cache(tmp_path: Path) -> None:
    """The flag is only worth anything on the row, which is where the worker reads it."""
    result = runner.invoke(
        application,
        [
            "enqueue",
            "video.transcript",
            "dQw4w9WgXcQ",
            "--refresh",
            "--data-dir",
            str(migrated(tmp_path)),
        ],
    )

    assert result.exit_code == 0, result.output
    with _database(tmp_path).session(readonly=True) as session:
        assert session.scalars(select(Job.refresh)).one() is True


def test_targets_can_come_from_a_file(tmp_path: Path) -> None:
    """A watch list is edited far more often than whatever reads it.

    Thirty ids written into a unit's ExecStart would mean editing the unit and
    reloading the manager to change one of them, so the schedule points at a
    file instead. Blank lines and `#` comments are how a list stays legible to
    the person maintaining it.
    """
    watchlist = tmp_path / "watchlist.txt"
    watchlist.write_text("# being sampled\ndQw4w9WgXcQ\n\n-2BFZsiVejU\n", encoding="utf-8")

    result = runner.invoke(
        application,
        [
            "enqueue",
            "video.transcript",
            "--from-file",
            str(watchlist),
            "--data-dir",
            str(migrated(tmp_path)),
        ],
    )

    assert result.exit_code == 0, result.output
    assert queued_targets(tmp_path) == ["dQw4w9WgXcQ", "-2BFZsiVejU"]


def test_a_watch_list_that_is_not_there_is_refused_rather_than_queueing_nothing(
    tmp_path: Path,
) -> None:
    """Silence is the wrong answer for something on a schedule.

    A timer firing hourly at a file someone moved would otherwise queue nothing,
    report success, and leave the series to stop moving with no failure anywhere
    for anyone to notice — which is the shape of bug this project keeps finding.
    """
    result = runner.invoke(
        application,
        [
            "enqueue",
            "video.transcript",
            "--from-file",
            str(tmp_path / "gone.txt"),
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    assert "gone.txt" in result.output + str(result.exception), (
        "the refusal did not name the list it could not read"
    )
    assert queued_targets(tmp_path) == []


def test_enqueue_with_no_targets_at_all_is_refused(tmp_path: Path) -> None:
    """Naming a kind and nothing to collect it for is a mistake, not an empty sweep."""
    result = runner.invoke(
        application, ["enqueue", "video.transcript", "--data-dir", str(tmp_path)]
    )

    assert result.exit_code != 0
    assert "no targets" in str(result.exception), result.output
    assert queued_targets(tmp_path) == []


def test_cancelling_a_queued_job_from_the_command_line(tmp_path: Path) -> None:
    runner.invoke(
        application,
        ["enqueue", "video.transcript", "dQw4w9WgXcQ", "--data-dir", str(migrated(tmp_path))],
    )
    with _database(tmp_path).session(readonly=True) as session:
        job_id = session.scalars(select(Job.identifier)).one()

    result = runner.invoke(application, ["cancel", job_id, "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    with _database(tmp_path).session(readonly=True) as session:
        assert session.scalars(select(Job.state)).one() == "cancelled"


def test_cancelling_a_job_that_does_not_exist_says_so_without_a_traceback(
    tmp_path: Path,
) -> None:
    migrated(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-m", "tubedepth.cli", "cancel", "0" * 32, "--data-dir", str(tmp_path)],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "no such job" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_work_once_takes_one_job_and_leaves_the_rest(tmp_path: Path) -> None:
    """`--once` had no test at all, which is how it kept its own code path.

    Rows are inserted rather than enqueued so the kinds are ones no source is
    registered for: the worker then fails them on the row without reaching the
    network, which is enough to count how many it took.
    """
    runner.invoke(
        application,
        ["enqueue", "video.transcript", "dQw4w9WgXcQ", "--data-dir", str(migrated(tmp_path))],
    )
    with _database(tmp_path).session() as session:
        original = session.scalars(select(Job)).one()
        original.kind = "video.notregistered"
        session.add(
            Job(
                kind=original.kind,
                target="second",
                state=original.state,
                attempt_count=original.attempt_count,
                max_attempts=original.max_attempts,
                webhook_attempts=original.webhook_attempts,
                refresh=original.refresh,
            )
        )

    result = runner.invoke(application, ["work", "--once", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    with _database(tmp_path).session(readonly=True) as session:
        states = dict(
            session.execute(select(Job.state, func.count()).group_by(Job.state)).tuples().all()
        )
    assert states == {"failed": 1, "queued": 1}, f"--once did not stop after one job: {states}"


def test_an_expensive_kind_is_queued_with_fewer_attempts_than_a_standard_one(
    tmp_path: Path,
) -> None:
    """`Job.max_attempts` says it is "set when the job is queued". Nothing set it.

    So a comment harvest — dozens of requests and minutes of wall clock by its
    own docstring — got the same three tries as a two-second metadata fetch,
    and an upstream failure spent three full harvests against one target out
    of the single per-address budget that caps this system.
    """
    migrated(tmp_path)
    runner.invoke(
        application, ["enqueue", "video.comments", "dQw4w9WgXcQ", "--data-dir", str(tmp_path)]
    )
    runner.invoke(
        application, ["enqueue", "video.metadata", "dQw4w9WgXcQ", "--data-dir", str(tmp_path)]
    )

    with _database(tmp_path).session(readonly=True) as session:
        attempts = dict(session.execute(select(Job.kind, Job.max_attempts)).tuples().all())

    assert attempts["video.comments"] < attempts["video.metadata"], (
        f"an expensive kind was queued with as many tries as a standard one: {attempts}"
    )


def test_recording_an_innertube_surface_nobody_records_is_refused_before_the_network(
    tmp_path: Path,
) -> None:
    """The refusal has to come before the request, not from the response.

    `browse-channel-about` is the surface deliberately left out, and it is also
    the one that has already broken once — so an operator asking for it should
    be told, not handed a half-right fixture for it.
    """
    result = runner.invoke(
        application,
        [
            "capture-fixture",
            "@someone",
            "--name",
            "2026-08-20-browse-channel-about",
            "--innertube",
            "browse-channel-about",
            "--into",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    assert "browse-channel-about" in str(result.exception)
    assert list(tmp_path.iterdir()) == []


def test_the_backfill_command_reports_what_it_attributed(tmp_path: Path) -> None:
    """A command nobody runs is the same as no command, so `migrate` points at
    this one — the window closes as retention ages out the rows it would
    attribute.

    Seeded straight through `_database()`, the same database
    `_cli_database_url` pointed `TUBEDEPTH_DATABASE_URL` at for this test.
    """
    from tubedepth.fingerprints import fingerprint
    from tubedepth.models import utcnow

    database = _database(tmp_path)
    database.create_schema()
    with database.session() as session:
        session.add(
            Artifact(
                kind="video.metadata",
                target="dQw4w9WgXcQ",
                fingerprint=fingerprint(
                    kind="video.metadata", target="dQw4w9WgXcQ", schema_version="1"
                ),
                digest="d" * 64,
                byte_count=1,
                fetched_at=utcnow(),
                fresh_until=utcnow(),
            )
        )

    result = runner.invoke(application, ["backfill-schema-versions", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    with _database(tmp_path).session(readonly=True) as session:
        assert session.scalars(select(Artifact.schema_version)).one() == "1"


def test_the_key_list_says_when_each_key_was_last_used(tmp_path: Path) -> None:
    """The one question asked before revoking, which had no answer."""
    from tubedepth.services.keys import ApiKeyService

    database = _database(tmp_path)
    database.create_schema()
    ApiKeyService(database).mint(label="ingest")

    result = runner.invoke(application, ["key", "list", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "ingest" in result.output
    assert "never used" in result.output


def test_the_worker_can_be_paused_from_the_command_line(tmp_path: Path) -> None:
    """The control was reachable only through the API, which is the wrong
    dependency: if the API is down, or was never installed, the worker is the
    process you most want to be able to stop and the one you cannot."""
    result = runner.invoke(
        application,
        ["pause", "--reason", "watching a quota", "--data-dir", str(migrated(tmp_path))],
    )

    assert result.exit_code == 0, result.output
    with _database(tmp_path).session(readonly=True) as session:
        control = session.scalars(select(WorkerControl)).one()
        assert (control.paused, control.reason) == (True, "watching a quota")


def test_resuming_from_the_command_line_says_what_changed(tmp_path: Path) -> None:
    runner.invoke(application, ["pause", "--data-dir", str(migrated(tmp_path))])

    result = runner.invoke(application, ["resume", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    with _database(tmp_path).session(readonly=True) as session:
        assert session.scalars(select(WorkerControl.paused)).one() is False


def test_the_key_list_shows_the_allowance_it_is_about_to_be_asked_for(tmp_path: Path) -> None:
    """`key create --rpm N` set an allowance the listing never showed back.

    When a client starts getting "over its allowance of N requests per minute",
    finding N for that key meant opening SQLite by hand — the exact complaint
    this command's own docstring makes about `last_used_at`, reintroduced in
    the command written to answer it.
    """
    from tubedepth.services.keys import ApiKeyService

    database = _database(tmp_path)
    database.create_schema()
    ApiKeyService(database).mint(label="ingest", requests_per_minute=240)

    result = runner.invoke(application, ["key", "list", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "240" in result.output, result.output


def test_prune_refuses_a_store_whose_index_is_somewhere_else(tmp_path: Path) -> None:
    """The half-finished cutover, from the command line.

    The payloads are on disk and the index the operator pointed at holds
    nothing. Sweeping here deletes the whole store, so the command fails and
    names the flag that means "no, this store really has no index".
    """
    migrated(tmp_path)
    (tmp_path / "payloads" / "video.metadata" / "ab").mkdir(parents=True)
    (tmp_path / "payloads" / "video.metadata" / "ab" / f"{'ab' * 32}.json.gz").write_bytes(b"x")

    result = runner.invoke(application, ["prune", "--data-dir", str(tmp_path)])

    # `main()` is what turns a TubedepthError into the "✗ …" line and exit 1;
    # CliRunner invokes the Typer app directly, one level inside that.
    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigurationError)
    assert "--sweep-without-an-index" in str(result.exception)


def test_prune_sweeps_an_indexless_store_when_told_to(tmp_path: Path) -> None:
    migrated(tmp_path)
    (tmp_path / "payloads" / "video.metadata" / "ab").mkdir(parents=True)
    (tmp_path / "payloads" / "video.metadata" / "ab" / f"{'ab' * 32}.json.gz").write_bytes(b"x")

    result = runner.invoke(
        application, ["prune", "--data-dir", str(tmp_path), "--sweep-without-an-index"]
    )

    assert result.exit_code == 0, result.output
