"""The command line's own behaviour, as opposed to the services beneath it.

Only the parts that are the CLI's job: argument parsing, and refusing input it
cannot honour. Anything that would reach YouTube belongs in the live contracts.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from tubedepth.cli import application

runner = CliRunner()


def queued_targets(data_directory: Path) -> list[str]:
    """Targets in the queue, and an empty list when there is no queue at all.

    A command that refuses its arguments should not have created a database,
    so "no such table" is the same answer as "no rows" for these tests.
    """
    with sqlite3.connect(data_directory / "tubedepth.db") as connection:
        try:
            return [row[0] for row in connection.execute("SELECT target FROM jobs")]
        except sqlite3.OperationalError:
            return []


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
        ["enqueue", "video.transcript", "-2BFZsiVejU", "--data-dir", str(tmp_path)],
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
        ["enqueue", "video.transcript", "dQw4w9WgXcQ", "--refresh", "--data-dir", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    with sqlite3.connect(tmp_path / "tubedepth.db") as connection:
        assert next(connection.execute("SELECT refresh FROM jobs"))[0] == 1


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
            str(tmp_path),
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
        ["enqueue", "video.transcript", "dQw4w9WgXcQ", "--data-dir", str(tmp_path)],
    )
    with sqlite3.connect(tmp_path / "tubedepth.db") as connection:
        job_id = next(connection.execute("SELECT identifier FROM jobs"))[0]

    result = runner.invoke(application, ["cancel", job_id, "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    with sqlite3.connect(tmp_path / "tubedepth.db") as connection:
        assert next(connection.execute("SELECT state FROM jobs"))[0] == "cancelled"


def test_cancelling_a_job_that_does_not_exist_says_so_without_a_traceback(
    tmp_path: Path,
) -> None:
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
        application, ["enqueue", "video.transcript", "dQw4w9WgXcQ", "--data-dir", str(tmp_path)]
    )
    with sqlite3.connect(tmp_path / "tubedepth.db") as connection:
        connection.execute(
            "UPDATE jobs SET kind = 'video.notregistered'",
        )
        connection.execute(
            "INSERT INTO jobs (identifier, kind, target, state, attempt_count, max_attempts,"
            " scheduled_at, created_at, webhook_attempts, refresh)"
            " SELECT '0' * 32, kind, 'second', state, attempt_count, max_attempts,"
            " scheduled_at, created_at, webhook_attempts, refresh FROM jobs"
        )

    result = runner.invoke(application, ["work", "--once", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    with sqlite3.connect(tmp_path / "tubedepth.db") as connection:
        states = dict(connection.execute("SELECT state, count(*) FROM jobs GROUP BY state"))
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
    runner.invoke(
        application, ["enqueue", "video.comments", "dQw4w9WgXcQ", "--data-dir", str(tmp_path)]
    )
    runner.invoke(
        application, ["enqueue", "video.metadata", "dQw4w9WgXcQ", "--data-dir", str(tmp_path)]
    )

    with sqlite3.connect(tmp_path / "tubedepth.db") as connection:
        attempts = dict(connection.execute("SELECT kind, max_attempts FROM jobs"))

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
