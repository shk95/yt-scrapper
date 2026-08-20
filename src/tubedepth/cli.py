"""The command line. The HTTP API will sit on the same service layer."""

from __future__ import annotations

import gzip
import json
import logging
import os
import signal
import threading
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
from .innertube.client import InnerTubeClient
from .models import WORKER_CONTROL_ID, Artifact, Job, JobState, WorkerControl, utcnow
from .observability import configure_logging
from .payload_store import PayloadStore
from .repositories import JobRepository
from .retention import RetentionPolicy, RetentionService
from .schema_versions import SchemaVersionBackfill
from .services.keys import ApiKeyService
from .sources import default_registry
from .sources.innertube_sources import RECORDABLE_SURFACES, record_surface
from .sources.registry import attempts_for
from .sources.ytdlp_runtime import LibraryYtdlpRuntime
from .worker import Worker

logger = logging.getLogger(__name__)

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
                max_attempts=attempts_for(source),
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
    poll: Annotated[
        float,
        typer.Option(
            "--poll",
            envvar="TUBEDEPTH_POLL_SECONDS",
            help="Stay up and check for work this often, in seconds. 0 drains once and exits.",
        ),
    ] = 0.0,
) -> None:
    """Drain the queue.

    Every job goes through the same registry the CLI's own `collect` uses, so
    there is one implementation of what each kind means rather than two.

    `--concurrency` is an upper bound, not a target. The rate controller
    narrows it whenever YouTube pushes back and widens it again while requests
    keep succeeding, so the effective figure is measured rather than chosen.

    `--poll` is what the service unit passes. Without it this drains once and
    exits, which is what a person running it by hand wants and what `--once`
    refines; with it the process stays up. The unit used to get the same effect
    from `Restart=always` and a ten-second `RestartSec`, at the cost of a full
    interpreter start every ten seconds against a queue that was usually empty
    — and, once this moves to the shared PostgreSQL, a new set of connections
    just as often. `Worker.serve` has the measurements.
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
    if once or poll <= 0:
        completed = worker.drain(limit=1 if once else None)
    else:
        completed = worker.serve(poll=poll, stop=_stopping_on_signals())
    typer.echo(f"✓ {completed} job(s) completed")


def _stopping_on_signals() -> threading.Event:
    """An event the usual stop signals set, so a drain in flight can finish.

    The unit sends SIGINT precisely so a running job is not abandoned to wait
    out its full lease before another worker can take it. Left to Python's
    default that arrives as a KeyboardInterrupt in whichever frame is running,
    which is not a place that can end a drain tidily.

    The handler is installed once and then removed, so a second signal reaches
    the default and kills the process: an operator who has already asked twice
    is not asking for the current job to finish.
    """
    stopping = threading.Event()

    def stop(number: int, frame: object) -> None:
        signal.signal(number, signal.SIG_DFL)
        logger.info("stopping after the current drain; signal again to stop now")
        stopping.set()

    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, stop)
    return stopping


def _set_paused(data_directory: Path, *, paused: bool, reason: str | None) -> None:
    database = _database(data_directory)
    with database.session() as session:
        control = session.get(WorkerControl, WORKER_CONTROL_ID) or WorkerControl(
            identifier=WORKER_CONTROL_ID
        )
        control.paused = paused
        control.reason = reason
        control.changed_at = utcnow()
        session.add(control)


@application.command()
def pause(
    reason: Annotated[
        str | None, typer.Option("--reason", help="Why, for whoever lifts it")
    ] = None,
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
) -> None:
    """Tell the worker to stop claiming.

    The same row `PATCH /v1/control` writes, reachable without the API — which
    is the wrong thing to depend on here. If the API is down, or was never
    installed, the worker is the process you most want to be able to stop and
    the one you could not.

    A job already running finishes: the extraction is inside yt-dlp and keeps
    spending requests until it is done. Cancel it if that is what you need.
    """
    _set_paused(data_directory, paused=True, reason=reason)
    typer.echo("✓ paused — the worker will claim nothing; work already running still finishes")


@application.command()
def resume(
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
) -> None:
    """Let the worker claim again.

    Nothing was failed or cancelled on the way in, so this is the whole of the
    undo — the queue is where it was left.
    """
    _set_paused(data_directory, paused=False, reason=None)
    typer.echo("✓ resumed")


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

    # A separate command is a command that gets skipped, and the window for
    # this one closes: attribution works by recomputing fingerprints against
    # the versions a kind has had, and retention ages out the rows it would
    # attribute. Say so here, where someone is already standing.
    with _database(data_directory).session(readonly=True) as session:
        unattributed = session.query(Artifact).filter(Artifact.schema_version.is_(None)).count()
    if unattributed:
        typer.echo(f"· {unattributed} artifact(s) do not name the schema version that wrote them")
        typer.echo("  run: tubedepth backfill-schema-versions")


@application.command(name="backfill-schema-versions")
def backfill_schema_versions(
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would change and write nothing")
    ] = False,
) -> None:
    """Say which normalizer wrote each payload collected before we recorded it.

    A fingerprint is a SHA-256 and gives nothing back, so attribution works
    forwards: recompute the hash for each version the kind could have been at
    and see which agrees. A match is a proof — the hash covers kind, target,
    version and parameters together — so nothing here guesses. A row that
    matches no candidate is left blank and reported by kind, because
    unattributed and honest beats attributed and wrong.

    Safe to run beside a busy worker, and safe to run twice: it selects only
    rows that do not name a version, and a row the worker writes names one.
    """
    outcome = SchemaVersionBackfill(database=_database(data_directory)).run(dry_run=dry_run)
    verb = "would attribute" if dry_run else "attributed"
    typer.echo(f"✓ {verb} {outcome.attributed} of {outcome.scanned} artifact(s)")
    for kind, count in sorted(outcome.unattributed.items()):
        typer.echo(f"· {count} {kind} artifact(s) matched no known version")
    if outcome.unattributed:
        typer.echo("  a version missing from PREVIOUS_VERSIONS looks exactly like this")


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


@keys_app.command("list")
def key_list(
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
) -> None:
    """What keys this instance has, and when each was last used.

    "Is anything still using this" is the question anyone asks before revoking,
    and until now it could not be answered: the column was written on every
    verified request and read by nothing, while the secret is shown once and
    cannot be worked out afterwards.

    Revoked keys stay listed. Jobs carry `api_key_id`, so a row that vanished
    would make every job that key submitted unattributable.
    """
    listed = ApiKeyService(_database(data_directory)).listed()
    if not listed:
        typer.echo("no keys; mint one with `tubedepth key create --label <name>`")
        return
    for entry in listed:
        used = entry.last_used_at.strftime("%Y-%m-%d %H:%M") if entry.last_used_at else "never used"
        state = " (revoked)" if entry.revoked else ""
        # The allowance is here because the error a client sees names it —
        # "over its allowance of N requests per minute" — and finding N for a
        # key otherwise means opening SQLite. `created_at` is what this listing
        # is ordered by, so showing it is what makes the order readable.
        typer.echo(
            f"{entry.identifier}  {entry.key_prefix}…  "
            f"created {entry.created_at:%Y-%m-%d}  {used:<16}  "
            f"{entry.requests_per_minute}/min  {entry.label}{state}"
        )


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
    directory: Annotated[Path | None, typer.Option("--into", help="Fixture directory")] = None,
    innertube: Annotated[
        str | None,
        typer.Option(
            "--innertube", help=f"Record a surface instead: {', '.join(RECORDABLE_SURFACES)}"
        ),
    ] = None,
) -> None:
    """Record a real response as a committable fixture.

    Reaches the network on purpose, and is run by a person rather than by CI.
    The redaction is the point: see fixture_capture for what is stripped and
    why the two rules differ.

    Without `--innertube` this records a yt-dlp dump. With it, an InnerTube
    surface — which had no command at all, so the fixtures under
    `tests/fixtures/innertube/` were made by hand and their redaction ran only
    if whoever made them remembered to call it.
    """
    if innertube is not None:
        directory = directory or Path("tests/fixtures/innertube")
        typer.echo(f"→ recording {innertube} for {target}")
        body = record_surface(innertube, target, caller=InnerTubeClient(DirectEgress()))
        summary = f"  top-level keys {len(body)}"
    else:
        directory = directory or Path("tests/fixtures/ytdlp/video_metadata")
        video_id = normalize_video_identifier(target)
        typer.echo(f"→ extracting {video_id}")
        dump = LibraryYtdlpRuntime().extract(video_id, egress=DirectEgress())
        body = redact_for_fixture(dump)
        summary = (
            f"  keys {len(dump)}  chapters {len(dump.get('chapters') or [])}"
            f"  heatmap {len(dump.get('heatmap') or [])}"
            f"  subtitle languages {len(dump.get('subtitles') or {})}"
        )

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json.gz"
    payload = json.dumps(body, ensure_ascii=False, indent=1, sort_keys=True)
    path.write_bytes(gzip.compress(payload.encode()))

    typer.echo(f"✓ wrote {path} ({path.stat().st_size / 1024:.0f} KB gzipped)")
    typer.echo(summary)


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
