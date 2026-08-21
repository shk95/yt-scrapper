"""The command line's own behaviour, as opposed to the services beneath it.

Only the parts that are the CLI's job: argument parsing, and refusing input it
cannot honour. Anything that would reach YouTube belongs in the live contracts.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
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


def queued_jobs(data_directory: Path) -> list[tuple[str, str, str | None, bool]]:
    """Every queued job as (kind, target, follow_up_kind, refresh).

    `queued_targets` above answers the question the `enqueue` tests ask — did
    this reach the queue at all. A watch list's whole point is that one line
    means a particular kind with a particular fan-out, so these tests need the
    other three columns as well.
    """
    try:
        with _database(data_directory).session(readonly=True) as session:
            return [
                (job.kind, job.target, job.follow_up_kind, job.refresh)
                for job in session.scalars(select(Job).order_by(Job.created_at, Job.identifier))
            ]
    except DBAPIError:
        return []


def written_watchlist(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "watchlist.txt"
    path.write_text(body, encoding="utf-8")
    return path


def until(condition: Callable[[], bool], seconds: float = 10.0) -> bool:
    """Wait for something a background thread is doing, or give up and say so.

    Bounded rather than unbounded, the same shape `tests/test_worker.py` uses:
    a watch loop that never comes round again should fail this file in seconds
    with an assertion, not hang the run.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_watch_queues_one_job_per_line_with_the_kind_that_line_names(tmp_path: Path) -> None:
    """The release gate in one test: a channel, a keyword and a region together.

    Three listing kinds in one schedule is the thing a bare-id list could not
    express — `UCxxx`, `kpop debut` and `KR` are three target types that no
    inspection of the string separates reliably. Each carries
    `video.metadata` as its follow-up, because a listing on its own enumerates
    and collects nothing.
    """
    watchlist = written_watchlist(
        tmp_path,
        "channel @director_pihyunjung\nsearch 케이팝 데뷔\ntrending KR\n",
    )

    result = runner.invoke(
        application, ["watch", str(watchlist), "--data-dir", str(migrated(tmp_path))]
    )

    assert result.exit_code == 0, result.output
    assert queued_jobs(tmp_path) == [
        ("channel.videos", "@director_pihyunjung", "video.metadata", True),
        ("search.videos", "케이팝 데뷔", "video.metadata", True),
        ("trending.videos", "KR", "video.metadata", True),
    ]


def test_every_job_watch_queues_is_forced_past_the_freshness_window(tmp_path: Path) -> None:
    """The listing lines too, which is the half that is easy to get wrong.

    Without the flag on the listing job the enumeration is answered from the
    cache and the sweep records nothing at all — not even the fan-out, since
    a cached listing still fans out but to the videos it held when it was
    collected. There is no per-line flag: a watch pass that did not force
    would be a watch pass that stops collecting after its first hour.
    """
    watchlist = written_watchlist(tmp_path, "video dQw4w9WgXcQ\nchannel @director_pihyunjung\n")

    result = runner.invoke(
        application, ["watch", str(watchlist), "--data-dir", str(migrated(tmp_path))]
    )

    assert result.exit_code == 0, result.output
    assert [job[3] for job in queued_jobs(tmp_path)] == [True, True]
    assert result.output.count("(forced)") == 2, result.output


def test_a_comments_directive_queues_the_listing_once_per_follow_up(tmp_path: Path) -> None:
    """`channel+comments` is two listing jobs: one fanning out to metadata,
    one fanning out to comments.

    Two jobs rather than one job with two follow-ups, because a job carries
    exactly one `follow_up_kind`. Only the first is forced: forcing both would
    run the enumeration twice a pass and append two near-identical rows to the
    listing's artifact history every hour. The second rides the cache the
    first just wrote — the observation series moves once per pass, and the
    comments fan-out reads the same enumeration.
    """
    watchlist = written_watchlist(
        tmp_path, "channel+comments @director_pihyunjung\nsearch+comments 화장품\n"
    )

    result = runner.invoke(
        application, ["watch", str(watchlist), "--data-dir", str(migrated(tmp_path))]
    )

    assert result.exit_code == 0, result.output
    assert queued_jobs(tmp_path) == [
        ("channel.videos", "@director_pihyunjung", "video.metadata", True),
        ("channel.videos", "@director_pihyunjung", "video.comments", False),
        ("search.videos", "화장품", "video.metadata", True),
        ("search.videos", "화장품", "video.comments", False),
    ]
    assert "✓ 4 job(s) queued" in result.output, result.output


def test_a_video_line_is_collected_directly_and_fans_out_to_nothing(tmp_path: Path) -> None:
    """A video is already the thing being collected, so a follow-up would be a
    second collection of the same video on every pass."""
    watchlist = written_watchlist(tmp_path, "video dQw4w9WgXcQ\n")

    result = runner.invoke(
        application, ["watch", str(watchlist), "--data-dir", str(migrated(tmp_path))]
    )

    assert result.exit_code == 0, result.output
    assert queued_jobs(tmp_path) == [("video.metadata", "dQw4w9WgXcQ", None, True)]


def test_a_watch_list_that_is_not_there_is_refused_rather_than_watching_nothing(
    tmp_path: Path,
) -> None:
    """The same rule `enqueue --from-file` follows, at the command that a timer
    actually fires: a list somebody moved must be a failure, not an empty pass
    reported as a success every hour while the history stops moving.

    Against a migrated data directory, unlike the `enqueue` test this mirrors.
    `watch` opens the database before it reads the list — it holds one
    connection across every pass rather than one per pass — so an unmigrated
    directory would be refused for that instead, and this test would pass
    without ever reaching the list.
    """
    result = runner.invoke(
        application, ["watch", str(tmp_path / "gone.txt"), "--data-dir", str(migrated(tmp_path))]
    )

    assert result.exit_code != 0
    assert "gone.txt" in result.output + str(result.exception), (
        "the refusal did not name the list it could not read"
    )
    assert queued_targets(tmp_path) == []


def test_an_unknown_directive_refuses_the_whole_pass_and_queues_nothing(tmp_path: Path) -> None:
    """A typo costs the pass, not the lines above it.

    Queueing the good lines and refusing the rest is the harder failure to
    see: the jobs that did run make the pass look like it worked, so the
    missing kind is only noticed by whoever goes looking for data that was
    never collected.
    """
    watchlist = written_watchlist(tmp_path, "video dQw4w9WgXcQ\nchannels @director_pihyunjung\n")

    result = runner.invoke(
        application, ["watch", str(watchlist), "--data-dir", str(migrated(tmp_path))]
    )

    assert result.exit_code != 0
    assert "line 2" in str(result.exception), result.output
    assert queued_targets(tmp_path) == []


def test_a_watch_list_with_every_line_commented_out_is_refused(tmp_path: Path) -> None:
    """A list that parses to nothing is a watcher watching nothing, which is
    the failure the required argument is shaped against — reached the other
    way round."""
    watchlist = written_watchlist(tmp_path, "# channel @director_pihyunjung\n\n")

    result = runner.invoke(
        application, ["watch", str(watchlist), "--data-dir", str(migrated(tmp_path))]
    )

    assert result.exit_code != 0
    assert "nothing to watch" in str(result.exception), result.output
    assert queued_targets(tmp_path) == []


def _watching(
    tmp_path: Path,
    watchlist: Path,
    *,
    every: str,
    stop: threading.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> threading.Thread:
    """`watch --every` on a thread, so the test can drive its stop event.

    The event is injected in place of `_stopping_on_signals`, which installs
    signal handlers and so only works on the main thread. That is the same
    seam `Worker.serve` already has — the worker tests pass their own event
    too — and everything under test here still goes through the real command.
    """
    monkeypatch.setattr("tubedepth.cli._stopping_on_signals", lambda: stop)
    thread = threading.Thread(
        target=lambda: runner.invoke(
            application,
            ["watch", str(watchlist), "--data-dir", str(tmp_path), "--every", every],
        ),
        daemon=True,
    )
    thread.start()
    return thread


def test_watch_with_an_interval_stays_up_and_queues_the_list_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--every` is the compose case: no timer, so the process is the schedule.

    Queueing the list twice is the whole claim — a one-shot that happened to
    exit zero would satisfy every other test in this file.
    """
    watchlist = written_watchlist(tmp_path, "video dQw4w9WgXcQ\n")
    migrated(tmp_path)
    stop = threading.Event()

    thread = _watching(tmp_path, watchlist, monkeypatch=monkeypatch, every="0.01", stop=stop)
    try:
        assert until(lambda: len(queued_targets(tmp_path)) >= 2), (
            "the second pass never ran, so `--every` did not stay resident"
        )
    finally:
        stop.set()
        thread.join(timeout=10)

    assert not thread.is_alive(), "watch did not return after its stop event was set"


def test_a_stop_while_watch_is_waiting_is_not_made_to_wait_out_the_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stop.wait(every)`, not `time.sleep(every)`.

    A sleeping watcher would still shut down — the unit allows time for it —
    but every stop would cost a full interval, and an hourly interval makes
    that indistinguishable from a hang.
    """
    watchlist = written_watchlist(tmp_path, "video dQw4w9WgXcQ\n")
    migrated(tmp_path)
    stop = threading.Event()

    thread = _watching(tmp_path, watchlist, monkeypatch=monkeypatch, every="3600", stop=stop)
    assert until(lambda: queued_targets(tmp_path) != []), "the first pass never ran"

    started = time.monotonic()
    stop.set()
    thread.join(timeout=10)
    elapsed = time.monotonic() - started

    assert not thread.is_alive(), "watch did not return after its stop event was set"
    assert elapsed < 5.0, f"an hourly interval made a stop take {elapsed:.1f}s"


def test_a_list_that_breaks_after_the_first_pass_is_logged_and_the_watcher_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-finished edit must not be what stops collection.

    The first pass fails fast — that is a misconfiguration, and the operator
    is watching. A later one is almost always someone editing the file, and
    exiting there would leave nothing collecting until somebody noticed. So
    the pass is skipped, loudly, and the next one reads the file again — which
    is also how an edit takes effect without a restart.
    """
    watchlist = written_watchlist(tmp_path, "video dQw4w9WgXcQ\n")
    migrated(tmp_path)
    stop = threading.Event()

    thread = _watching(tmp_path, watchlist, monkeypatch=monkeypatch, every="0.01", stop=stop)
    try:
        assert until(lambda: queued_targets(tmp_path) != []), "the first pass never ran"
        watchlist.write_text("channels @director_pihyunjung\n", encoding="utf-8")
        broken = len(queued_targets(tmp_path))
        assert until(lambda: len(queued_targets(tmp_path)) == broken), "the queue never settled"
        assert thread.is_alive(), "a broken edit killed the watcher instead of being logged"

        watchlist.write_text("video dQw4w9WgXcQ\nvideo 9bZkp7q19f0\n", encoding="utf-8")
        assert until(lambda: "9bZkp7q19f0" in queued_targets(tmp_path)), (
            "the list was not read again, so an edit needs a restart"
        )
    finally:
        stop.set()
        thread.join(timeout=10)


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


def test_transfer_dry_run_checks_the_source_placement_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--dry-run` returns before ever calling `transfer()` — the only other
    caller of `verify_placement()` on this path — so without its own call, a
    PostgreSQL source on the wrong `search_path` would print six confident
    zeros (every table genuinely empty *from this connection*) instead of the
    refusal an operator needs to see before trusting what it reports."""
    monkeypatch.setattr(
        Database,
        "verify_placement",
        lambda self: (_ for _ in ()).throw(ConfigurationError("wrong search_path")),
    )

    result = runner.invoke(
        application,
        [
            "transfer",
            "--from",
            f"sqlite+pysqlite:///{tmp_path / 'source.db'}",
            "--to",
            "postgresql+psycopg://u:p@h:5432/db",
            "--dry-run",
        ],
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigurationError)
    assert "wrong search_path" in str(result.exception)


def _the_password() -> str:
    """The password inside `TUBEDEPTH_DATABASE_URL`, which must never reach
    output. `_cli_database_url` points every test here at the migrator
    credential — the one that can issue DDL, which is what makes #30 worth a
    regression test rather than a cosmetic one."""
    password = make_url(database_url()).password
    assert password, "these tests need a URL that actually carries a password"
    return password


def test_migrate_masks_the_password_in_its_success_line(tmp_path: Path) -> None:
    """#30: `tubedepth migrate` echoed the full URL, password included — into
    shell scrollback, journalctl, and `docker compose logs`, none of which are
    places a DDL credential belongs."""
    password = _the_password()

    result = runner.invoke(application, ["migrate", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert f":{password}@" not in result.output, "the migrator password reached stdout"
    assert "***" in result.output, "the URL should still be shown, just masked"


def test_migrate_stamp_masks_the_password_too(tmp_path: Path) -> None:
    """The other echo in the same command — `--stamp` prints the URL as well."""
    password = _the_password()

    result = runner.invoke(application, ["migrate", "--stamp", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert f":{password}@" not in result.output
    assert "***" in result.output


def test_the_no_schema_refusal_masks_the_password(tmp_path: Path) -> None:
    """The same URL reaches `_database()`'s `no schema at {url}` refusal, which
    `main()` prints for every command — so the masking has to live in one
    helper both call, not inline at `migrate`'s echoes."""
    password = _the_password()

    result = runner.invoke(application, ["jobs", "--data-dir", str(tmp_path)])

    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigurationError)
    message = str(result.exception)
    assert "no schema at" in message and "tubedepth migrate" in message
    assert f":{password}@" not in message, "the password reached an error message"
    assert "***" in message


def test_transfer_masks_the_target_password_in_its_no_schema_refusal(tmp_path: Path) -> None:
    """`transfer` has its own copy of the no-schema refusal, formatted from
    `--to` — a URL that in a real cutover carries the runtime credential."""
    password = _the_password()
    source_url = f"sqlite+pysqlite:///{tmp_path / 'source.db'}"
    Database(source_url, allow_sqlite_source=True).create_schema()

    result = runner.invoke(
        application,
        ["transfer", "--from", source_url, "--to", database_url()],
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigurationError), result.output
    message = str(result.exception)
    assert "no schema at" in message
    assert f":{password}@" not in message, "the password reached an error message"
    assert "***" in message


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


class TestFlatten:
    """`tubedepth flatten`, end to end through the CLI.

    One video.metadata artifact and its payload, arranged the way collection
    actually leaves them: a payload file under the store's content address,
    and an artifact row in the index pointing at that digest. `fetched_at` is
    set an hour in the past so `FlattenService`'s five-minute safety lag never
    holds it back from a pass run moments after the arrange step.
    """

    @staticmethod
    def _store_a_metadata_artifact(data_directory: Path) -> None:
        import json

        from tubedepth.fingerprints import fingerprint
        from tubedepth.models import utcnow
        from tubedepth.payload_store import PayloadStore

        payload = {
            "video_id": "dQw4w9WgXcQ",
            "title": "Never Gonna Give You Up",
            "channel": "Rick Astley",
            "channel_id": "UCuAXFkgsw1L7xaCfnd5JJOw",
            "view_count": 1_000_000,
        }
        stored = PayloadStore(data_directory / "payloads").put(
            "video.metadata", json.dumps(payload).encode()
        )
        database = _database(data_directory)
        database.create_schema()
        fetched_at = utcnow() - timedelta(hours=1)
        with database.session() as session:
            session.add(
                Artifact(
                    kind="video.metadata",
                    target="dQw4w9WgXcQ",
                    fingerprint=fingerprint(
                        kind="video.metadata", target="dQw4w9WgXcQ", schema_version="1"
                    ),
                    digest=stored.digest,
                    byte_count=stored.byte_count,
                    fetched_at=fetched_at,
                    fresh_until=fetched_at + timedelta(hours=6),
                )
            )

    def test_flattens_what_the_worker_stored(self, tmp_path: Path) -> None:
        self._store_a_metadata_artifact(tmp_path)

        result = runner.invoke(application, ["flatten", "--data-dir", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "flattened" in result.output
        with _database(tmp_path).session(readonly=True) as session:
            from tubedepth.models import VideoSnapshot

            snapshots = list(session.scalars(select(VideoSnapshot)))
        assert len(snapshots) == 1
        assert snapshots[0].video_id == "dQw4w9WgXcQ"

    def test_dry_run_reports_without_writing(self, tmp_path: Path) -> None:
        self._store_a_metadata_artifact(tmp_path)

        result = runner.invoke(application, ["flatten", "--data-dir", str(tmp_path), "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "would flatten" in result.output
        with _database(tmp_path).session(readonly=True) as session:
            from tubedepth.models import VideoSnapshot

            assert session.scalar(select(func.count()).select_from(VideoSnapshot)) == 0

    def test_a_second_pass_is_a_no_op(self, tmp_path: Path) -> None:
        self._store_a_metadata_artifact(tmp_path)
        first = runner.invoke(application, ["flatten", "--data-dir", str(tmp_path)])
        assert first.exit_code == 0, first.output

        second = runner.invoke(application, ["flatten", "--data-dir", str(tmp_path)])

        assert second.exit_code == 0, second.output
        assert "flattened 0 of 0 artifact(s)" in second.output

    def test_a_batch_or_limit_below_one_is_refused(self, tmp_path: Path) -> None:
        # Both used to be accepted and both reported a clean pass having done
        # nothing at all — the one failure an operator cannot see.
        for option in ("--batch", "--limit"):
            result = runner.invoke(
                application, ["flatten", "--data-dir", str(tmp_path), option, "0"]
            )
            assert result.exit_code != 0, f"{option} 0 was accepted: {result.output}"
