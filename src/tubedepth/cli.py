"""The command line. The HTTP API will sit on the same service layer."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .collection import CollectionService
from .database import Database
from .egress.transport import DirectEgress
from .errors import TubedepthError
from .fixture_capture import redact_for_fixture
from .identifiers import normalize_video_identifier
from .models import Job
from .observability import configure_logging
from .payload_store import PayloadStore
from .sources.ytdlp_runtime import LibraryYtdlpRuntime
from .worker import Worker

application = typer.Typer(
    name="tubedepth",
    help="Collect the YouTube data the official Data API does not expose.",
    no_args_is_help=True,
)


def _payload_store(data_directory: Path) -> PayloadStore:
    return PayloadStore(data_directory / "payloads")


@application.command()
def collect(
    kind: Annotated[str, typer.Argument(help="What to collect; see `tubedepth sources`")],
    target: Annotated[str, typer.Argument(help="A video URL or bare video id")],
    data_directory: Annotated[
        Path,
        typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR", help="Where payloads are stored"),
    ] = Path("var"),
    show: Annotated[bool, typer.Option("--show/--no-show", help="Print the payload")] = False,
) -> None:
    """Collect one kind of data for one video and store it."""
    payloads = _payload_store(data_directory)
    service = CollectionService(payloads=payloads)

    typer.echo(f"→ collecting {kind} for {target}")
    collected = service.collect(kind, target)
    typer.echo(f"✓ stored {collected.payload.byte_count} bytes at {collected.payload.path}")
    if show:
        body = json.loads(payloads.read(collected.payload.digest))
        typer.echo(json.dumps(body, indent=2, ensure_ascii=False))


def _database(data_directory: Path) -> Database:
    data_directory.mkdir(parents=True, exist_ok=True)
    database = Database(data_directory / "tubedepth.db")
    database.create_schema()
    return database


@application.command()
def enqueue(
    kind: Annotated[str, typer.Argument(help="What to collect; see `tubedepth sources`")],
    targets: Annotated[list[str], typer.Argument(help="One or more video URLs or ids")],
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
) -> None:
    """Queue work without doing it. The worker picks it up."""
    database = _database(data_directory)
    with database.session() as session:
        for target in targets:
            job = Job(kind=kind, target=normalize_video_identifier(target))
            session.add(job)
            session.flush()
            typer.echo(f"→ queued {job.identifier}  {kind}  {job.target}")


@application.command()
def work(
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
    once: Annotated[bool, typer.Option("--once", help="Take one job and stop")] = False,
) -> None:
    """Drain the queue.

    Every job goes through the same registry the CLI's own `collect` uses, so
    there is one implementation of what each kind means rather than two.
    """
    configure_logging()
    worker = Worker(
        database=_database(data_directory),
        payloads=_payload_store(data_directory),
        name=f"cli-{os.getpid()}",
    )
    completed = 1 if (once and worker.run_once()) else (0 if once else worker.drain())
    typer.echo(f"✓ {completed} job(s) completed")


@application.command()
def jobs(
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
) -> None:
    """Show the queue."""
    with _database(data_directory).session() as session:
        rows = session.query(Job).order_by(Job.created_at).all()
        for job in rows:
            detail = job.error_message or (
                f"{job.payload_bytes} bytes" if job.payload_bytes else ""
            )
            typer.echo(
                f"{job.identifier[:8]}  {job.state.value:<9}  "
                f"{job.kind:<18}  {job.target:<14}  {detail}"
            )


@application.command()
def sources() -> None:
    """List what this build can collect.

    Read from the registry, so a newly added source appears here without this
    command being touched.
    """
    for kind in CollectionService(payloads=_payload_store(Path("var"))).kinds():
        typer.echo(kind)


@application.command(name="capture-fixture")
def capture_fixture(
    target: Annotated[str, typer.Argument(help="A video URL or bare video id")],
    name: Annotated[str, typer.Option("--name", help="Filename stem, dated by convention")],
    directory: Annotated[Path, typer.Option("--into", help="Fixture directory")] = Path(
        "tests/fixtures/ytdlp/video_metadata"
    ),
) -> None:
    """Record a real yt-dlp dump as a committable fixture.

    Reaches the network on purpose, and is run by a person rather than by CI.
    The redaction is the point: see fixture_capture for what is stripped and
    why the two rules differ.
    """
    video_id = normalize_video_identifier(target)
    typer.echo(f"→ extracting {video_id}")
    dump = LibraryYtdlpRuntime().extract(video_id, egress=DirectEgress())

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json.gz"
    payload = json.dumps(redact_for_fixture(dump), ensure_ascii=False, indent=1, sort_keys=True)
    path.write_bytes(gzip.compress(payload.encode()))

    typer.echo(f"✓ wrote {path} ({path.stat().st_size / 1024:.0f} KB gzipped)")
    typer.echo(
        f"  keys {len(dump)}  chapters {len(dump.get('chapters') or [])}"
        f"  heatmap {len(dump.get('heatmap') or [])}"
        f"  subtitle languages {len(dump.get('subtitles') or {})}"
    )


@application.command()
def version() -> None:
    """Print the app version and the yt-dlp it is actually running.

    The first question when extraction breaks is which yt-dlp this is, and the
    answer is not whatever `yt-dlp --version` on PATH says: everything here
    runs the version uv.lock pins.
    """
    from yt_dlp import version as ytdlp_version

    typer.echo(f"tubedepth {__version__}")
    typer.echo(f"yt-dlp    {ytdlp_version.__version__}")


def main() -> None:
    try:
        application()
    except TubedepthError as error:
        # SystemExit, not typer.Exit: typer.Exit is only meaningful inside
        # typer's own invocation, and raising it here printed the traceback
        # this handler exists to replace.
        typer.echo(f"✗ {error}", err=True)
        raise SystemExit(1) from error


if __name__ == "__main__":
    os.environ.setdefault("COLUMNS", "100")
    main()
