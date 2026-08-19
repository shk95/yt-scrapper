"""The command line. The HTTP API will sit on the same service layer."""

from __future__ import annotations

import gzip
import json
import os
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .api.application import create_application
from .collection import CollectionService
from .database import Database
from .egress.control import RateController
from .egress.transport import DirectEgress
from .errors import TubedepthError, ValidationError
from .fixture_capture import redact_for_fixture
from .identifiers import normalize_target, normalize_video_identifier
from .models import Job, JobState
from .observability import configure_logging
from .payload_store import PayloadStore
from .repositories import JobRepository
from .retention import RetentionPolicy, RetentionService
from .services.keys import ApiKeyService
from .sources import default_registry
from .sources.ytdlp_runtime import LibraryYtdlpRuntime
from .worker import Worker

application = typer.Typer(
    name="tubedepth",
    help="Collect the YouTube data the official Data API does not expose.",
    no_args_is_help=True,
)


def _payload_store(data_directory: Path) -> PayloadStore:
    return PayloadStore(data_directory / "payloads")


# Video, channel and playlist ids are base64url, so roughly one in a hundred
# begins with `-` and click reads it as an option: `No such option: -2`, naming
# neither the id nor the video it came from. These commands therefore take
# unknown options as arguments.
TOLERATE_LEADING_DASHES = {"ignore_unknown_options": True}


def _reject_option_like(values: Sequence[str]) -> None:
    """Refuse anything that is obviously a mistyped option, not an id.

    The setting above is what lets `--thn video.metadata` become two targets
    and a hundred jobs that can only fail. No id has ever started with two
    dashes, so shape alone separates the two cases.
    """
    for value in values:
        if value.startswith("--"):
            raise ValidationError(f"unknown option: {value}")


def _targets_from_file(path: Path) -> list[str]:
    """One target per line. Blank lines are skipped, and so are `#` comments.

    A schedule points at a file rather than carrying the list itself, because
    the list is edited far more often than whatever reads it — thirty ids on a
    unit's ExecStart line would mean editing the unit and reloading the manager
    to change one of them.

    A file that cannot be read is refused rather than treated as empty. A timer
    firing hourly at a watch list somebody moved would otherwise queue nothing,
    report success, and leave the history to stop moving with no failure
    anywhere for anyone to notice.

    Only a line whose first character is `#` is a comment, so a search query
    holding one survives. A query that *begins* with `#` has to be an argument
    instead; that is the price of the list being commentable at all.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValidationError(f"cannot read the target list at {path}: {error}") from error
    return [
        stripped for line in lines if (stripped := line.strip()) and not stripped.startswith("#")
    ]


@application.command(context_settings=TOLERATE_LEADING_DASHES)
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
    _reject_option_like([kind, target])
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


@application.command(context_settings=TOLERATE_LEADING_DASHES)
def enqueue(
    kind: Annotated[str, typer.Argument(help="What to collect; see `tubedepth sources`")],
    targets: Annotated[
        list[str] | None, typer.Argument(help="Videos, channels, playlists or a query")
    ] = None,
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
    then: Annotated[
        str | None,
        typer.Option("--then", help="For a listing kind: what to collect per video found"),
    ] = None,
    from_file: Annotated[
        Path | None,
        typer.Option("--from-file", help="Read targets from a file, one per line"),
    ] = None,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Collect even if a fresh artifact is held"),
    ] = False,
) -> None:
    """Queue work without doing it. The worker picks it up.

    With `--then`, one queued channel becomes a job per video it holds. That is
    the difference between enumerating and collecting at any volume.

    With `--refresh`, the job collects even where a fresh artifact is already
    held. That is what a repeated sweep of the same videos needs to be worth
    running: without it, anything asked for again inside its freshness window
    is answered from the cache and records no new observation, so the history
    the artifact table keeps simply stops moving.
    """
    from_list = _targets_from_file(from_file) if from_file is not None else []
    wanted = [*(targets or []), *from_list]
    _reject_option_like([kind, *wanted])
    if not wanted:
        raise ValidationError("no targets: name them as arguments, or point --from-file at a list")
    registry = default_registry()
    source = registry.get(kind)
    if then is not None:
        # Fail on a typo now rather than after a hundred jobs exist that can
        # only ever fail.
        registry.get(then)

    database = _database(data_directory)
    with database.session() as session:
        for target in wanted:
            job = Job(
                kind=kind,
                target=normalize_target(source.target_type, target),
                follow_up_kind=then,
                refresh=refresh,
            )
            session.add(job)
            session.flush()
            suffix = f" → {then}" if then else ""
            forced = " (forced)" if refresh else ""
            typer.echo(f"→ queued {job.identifier[:8]}  {kind}  {job.target}{suffix}{forced}")


@application.command()
def cancel(
    job_id: Annotated[str, typer.Argument(help="The job to stop; see `tubedepth jobs`")],
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
) -> None:
    """Stop a job that is no longer wanted.

    A queued job is cancelled outright. A running one is only marked: its
    extraction is inside yt-dlp and cannot be interrupted, so what this buys is
    that it will not be retried and will not hand back a result. The line this
    prints says which of the two happened, because the difference is whether
    requests are still going out.
    """
    database = _database(data_directory)
    with database.session() as session:
        job = JobRepository(session).cancel(job_id)
        if job.state is JobState.CANCELLED:
            typer.echo(f"✓ cancelled {job.identifier[:8]}  {job.kind}  {job.target}")
        else:
            typer.echo(
                f"→ marked {job.identifier[:8]}  {job.kind}  {job.target} — "
                "already running, so it will finish or fail on its own and keep no result"
            )


@application.command()
def work(
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
    webhook_secret: Annotated[
        str | None,
        typer.Option(
            "--webhook-secret",
            envvar="TUBEDEPTH_WEBHOOK_SECRET",
            help="Sign job callbacks with this. Without it, none are sent.",
        ),
    ] = None,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            "-c",
            envvar="TUBEDEPTH_CONCURRENCY",
            help="How many jobs may run at once, subject to the measured rate limit",
        ),
    ] = 4,
    once: Annotated[bool, typer.Option("--once", help="Take one job and stop")] = False,
) -> None:
    """Drain the queue.

    Every job goes through the same registry the CLI's own `collect` uses, so
    there is one implementation of what each kind means rather than two.

    `--concurrency` is an upper bound, not a target. The rate controller
    narrows it whenever YouTube pushes back and widens it again while requests
    keep succeeding, so the effective figure is measured rather than chosen.
    """
    configure_logging()
    worker = Worker(
        database=_database(data_directory),
        payloads=_payload_store(data_directory),
        name=f"cli-{os.getpid()}",
        concurrency=concurrency,
        webhook_secret=webhook_secret,
        controller=RateController(
            # The ceiling additive increase may not pass. Raising it is how an
            # operator asks for more throughput; the controller still refuses
            # to stay there if YouTube pushes back.
            window_ceiling=float(os.environ.get("TUBEDEPTH_WINDOW_CEILING", "6"))
        ),
    )
    completed = worker.drain(limit=1 if once else None)
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
def migrate(
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
    stamp: Annotated[
        bool,
        typer.Option("--stamp", help="Record the current revision without running anything"),
    ] = False,
) -> None:
    """Bring the database up to the current schema.

    `--stamp` is for the one-time case every project meets exactly once: a
    database that predates migrations. Upgrading it would try to create tables
    that are already there, so instead it records which revision its schema
    already matches and migrates forward from then on.

    `create_schema` still runs on startup and still adds nullable columns and
    missing indexes. That is a development convenience and this is the
    deployment path; where they disagree, a test says so.
    """
    from alembic import command
    from alembic.config import Config

    data_directory.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parent.parent.parent
    configuration = Config(str(root / "alembic.ini"))
    configuration.set_main_option("script_location", str(root / "migrations"))
    os.environ["TUBEDEPTH_DATABASE_URL"] = f"sqlite+pysqlite:///{data_directory / 'tubedepth.db'}"

    if stamp:
        command.stamp(configuration, "head")
        typer.echo(f"✓ stamped {data_directory / 'tubedepth.db'} at the current revision")
        return
    command.upgrade(configuration, "head")
    typer.echo(f"✓ {data_directory / 'tubedepth.db'} is at the current schema")


@application.command()
def prune(
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
    maximum_age_days: Annotated[
        int, typer.Option("--max-age-days", envvar="TUBEDEPTH_MAX_AGE_DAYS")
    ] = 30,
    maximum_gigabytes: Annotated[float, typer.Option("--max-gb", envvar="TUBEDEPTH_MAX_GB")] = 50.0,
) -> None:
    """Remove artifacts past their retention age.

    The size limit is a backstop rather than a target: nothing here tries to
    fill it, and reaching it means the age policy is not keeping up, so it is
    reported rather than quietly absorbed by evicting whatever is nearest.
    """
    configure_logging()
    outcome = RetentionService(
        database=_database(data_directory),
        payloads=_payload_store(data_directory),
        policy=RetentionPolicy(
            maximum_age=timedelta(days=maximum_age_days),
            maximum_bytes=int(maximum_gigabytes * 1024**3),
        ),
    ).prune()

    typer.echo(
        f"✓ removed {outcome.artifacts_removed} artifact(s), "
        f"freeing {outcome.bytes_removed / 1024**2:.1f} MiB"
    )
    if outcome.orphans_removed:
        typer.echo(f"  swept {outcome.orphans_removed} payload file(s) with no artifact row")
    typer.echo(f"  store is now {outcome.total_bytes / 1024**2:.1f} MiB on disk")
    if outcome.over_ceiling:
        typer.echo(
            f"✗ over the {maximum_gigabytes:.0f} GiB ceiling — "
            "the retention age is too generous for what is being collected",
            err=True,
        )
        raise SystemExit(1)


keys_app = typer.Typer(name="key", help="Manage API keys.", no_args_is_help=True)
application.add_typer(keys_app, name="key")


@keys_app.command("create")
def key_create(
    label: Annotated[str, typer.Option("--label", help="What this key is for")],
    requests_per_minute: Annotated[int, typer.Option("--rpm")] = 60,
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
) -> None:
    """Mint a key. The secret is printed once and is not recoverable."""
    minted = ApiKeyService(_database(data_directory)).mint(
        label=label, requests_per_minute=requests_per_minute
    )
    typer.echo(f"✓ {minted.identifier}  {minted.label}")
    typer.echo(f"  {minted.secret}")
    typer.echo("  Store it now — nothing here keeps a copy.")


@keys_app.command("revoke")
def key_revoke(
    identifier: Annotated[str, typer.Argument(help="The key identifier, not the secret")],
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
) -> None:
    """Revoke a key. Takes effect on the next request, not the next restart."""
    ApiKeyService(_database(data_directory)).revoke(identifier)
    typer.echo(f"✓ revoked {identifier}")


@application.command()
def serve(
    host: Annotated[str, typer.Option("--host", envvar="TUBEDEPTH_HOST")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", envvar="TUBEDEPTH_PORT")] = 8080,
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
) -> None:
    """Serve the HTTP API.

    Binds to localhost unless told otherwise: a default that exposes a port is
    found by a scanner before its owner notices. The worker runs separately —
    this process only queues and reads.
    """
    import uvicorn

    configure_logging()
    uvicorn.run(
        create_application(
            database=_database(data_directory), payloads=_payload_store(data_directory)
        ),
        host=host,
        port=port,
        log_config=None,
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
