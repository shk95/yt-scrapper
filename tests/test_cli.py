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
