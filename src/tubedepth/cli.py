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
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import __version__
from .api.application import create_application
from .collection import CollectionService
from .database import _MAX_OVERFLOW, _POOL_SIZE, Database, masked_url
from .egress.control import RateController
from .egress.transport import DirectEgress
from .errors import ConfigurationError, TubedepthError, ValidationError
from .fixture_capture import redact_for_fixture
from .flatten import FlattenService
from .identifiers import normalize_target, normalize_video_identifier
from .innertube.client import InnerTubeClient
from .models import WORKER_CONTROL_ID, Artifact, Base, Job, JobState, WorkerControl, utcnow
from .observability import configure_logging
from .payload_store import PayloadStore
from .repositories import JobRepository
from .retention import RetentionPolicy, RetentionService
from .schema_versions import SchemaVersionBackfill
from .services.keys import ApiKeyService
from .settings import database_url
from .sources import default_registry
from .sources.innertube_sources import RECORDABLE_SURFACES, record_surface
from .sources.registry import SourceRegistry, attempts_for
from .sources.ytdlp_runtime import LibraryYtdlpRuntime
from .transfer import mapped_models, transfer, verify_source_schema
from .watchlist import read_watchlist
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

    This is `enqueue --from-file`'s format and only that: the kind is an
    argument there, so a line carries a target and nothing else. It is not the
    format `tubedepth watch` reads — that one is typed, a directive per line,
    and lives in `watchlist.py`. Two files, two readers, on purpose.

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


def _database(
    data_directory: Path,
    *,
    pool_size: int = _POOL_SIZE,
    max_overflow: int = _MAX_OVERFLOW,
    allow_sqlite_source: bool = False,
) -> Database:
    """Open the database every CLI entry point uses.

    Creates no schema (#14) — but a database with none is not a database
    this can do anything useful against, and letting the first query fail
    with `no such table` inside SQLAlchemy is a traceback, not a refusal.
    `is_migrated()` only reflects, so checking it here does not reintroduce
    DDL on the boot path; it just turns the eventual failure into one that
    names the fix.

    `verify_placement()` runs first, ahead of `is_migrated()`: on the wrong
    `search_path`, `is_migrated()` would also see no `jobs` table — the
    schema this connection can reach is the wrong one, or none — and telling
    an operator to run `tubedepth migrate` would be the wrong diagnosis when
    the real schema already exists, fully migrated, just unreachable from
    here. Placement is a precondition for the migration check meaning
    anything at all, so it is asked first (#16).

    The URL comes from `settings.database_url`, the one resolver Alembic also
    calls (`migrate` below) — so `tubedepth work` and `tubedepth migrate`
    always agree on which database they mean. `data_directory` no longer
    contributes to that URL at all (there is no SQLite fallback under it to
    contribute, since the cutover, #15); it is created here only because most
    callers of `_database()` also open the payload store under the same path.

    `allow_sqlite_source` exists for exactly one caller: `migrate`'s
    post-upgrade attribution count, so that migrating an old SQLite file
    forward — the remedy `tubedepth transfer`'s preflight names (#33) —
    succeeds end to end instead of printing `✓` and then exiting non-zero
    from its own follow-up query. Nothing that *runs* the service passes it;
    the PostgreSQL-only refusal in `Database` stands for every other command.
    """
    data_directory.mkdir(parents=True, exist_ok=True)
    url = database_url()
    database = Database(
        url,
        allow_sqlite_source=allow_sqlite_source,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )
    database.verify_placement()
    if not database.is_migrated():
        # The URL is printed masked (#30): this message reaches terminals and
        # unit logs for every command, and the credential in it can issue DDL.
        raise ConfigurationError(
            f"no schema at {masked_url(url)} — run: tubedepth migrate --data-dir {data_directory}"
        )
    return database


def _queue_job(
    session: Session,
    registry: SourceRegistry,
    *,
    kind: str,
    target: str,
    then: str | None,
    refresh: bool,
) -> Job:
    """Put one job in the queue and say so on stdout.

    One implementation for every command that queues, because there are two of
    them now (`enqueue` and `watch`) and the parts worth getting wrong are the
    same in both: which normalizer the target goes through — a channel handle
    run through the video one is refused, a video id run through the channel
    one is accepted and fails inside the extractor minutes later — and how many
    attempts the job gets, which is a property of the source's cost rather than
    of the caller. A second copy is a second place for those to drift.
    """
    source = registry.get(kind)
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
    return job


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
    registry.get(kind)
    if then is not None:
        # Fail on a typo now rather than after a hundred jobs exist that can
        # only ever fail.
        registry.get(then)

    database = _database(data_directory)
    with database.session() as session:
        for target in wanted:
            _queue_job(session, registry, kind=kind, target=target, then=then, refresh=refresh)


def _watch_pass(database: Database, registry: SourceRegistry, watchlist: Path) -> int:
    """Read the list and queue every line of it, forced. Returns how many.

    The list is re-read here rather than once at startup, so an operator
    editing it does not have to restart anything for the next pass to pick the
    change up. That is also why parsing lives inside the pass: a broken edit is
    a broken pass, not a broken process.

    A list that parses to nothing is refused. `watch` takes its list as a
    required argument for the same reason — a watcher that watches nothing
    while reporting success every hour is the failure this whole command is
    shaped against, and a file whose lines have all been commented out is
    exactly that.
    """
    directives = read_watchlist(watchlist)
    if not directives:
        raise ValidationError(f"nothing to watch: {watchlist} holds no directives")

    # `TOLERATE_LEADING_DASHES` is deliberately not set on this command. Its
    # only positional is a path, so nothing click parses here is a base64url
    # id that could begin with a dash — the reason the other commands accept
    # unknown options as arguments does not apply. The targets arrive from the
    # file instead, where click cannot swallow them; but `channel --then
    # video.metadata`, pasted out of an `enqueue` command line, is still a typo
    # rather than a channel, so the shape rule `_reject_option_like` uses is
    # applied here too — with the line number the file makes available, which
    # is the one thing that helper cannot say.
    for directive in directives:
        if directive.target.startswith("--"):
            raise ValidationError(
                f"{watchlist} line {directive.line}: "
                f"{directive.target!r} is an option, not a target"
            )

    queued = 0
    with database.session() as session:
        for directive in directives:
            # A job carries exactly one `follow_up_kind`, so a directive with
            # two follow-ups (`channel+comments`) is two listing jobs.
            for index, follow_up in enumerate(directive.follow_ups or (None,)):
                _queue_job(
                    session,
                    registry,
                    kind=directive.kind,
                    target=directive.target,
                    then=follow_up,
                    # One forced enqueue per line, with no per-line flag and
                    # nothing to get wrong. Without it a sweep inside the
                    # freshness window is answered from the cache and records no
                    # observation, so the series a watch list exists to build
                    # simply stops moving. On a listing this re-runs the
                    # enumeration; the per-video follow-ups it fans out to stay
                    # cache-governed, because `Worker._queue_follow_up`
                    # deliberately does not propagate the flag.
                    #
                    # Only the *first* job of a multi-follow-up line is forced.
                    # Forcing every one would run the same enumeration once per
                    # follow-up and append near-identical rows to the listing's
                    # history each pass; unforced, the later jobs ride the cache
                    # the first just wrote and fan out the same videos.
                    refresh=index == 0,
                )
                queued += 1
    return queued


@application.command()
def watch(
    watchlist: Annotated[
        Path,
        typer.Argument(
            envvar="TUBEDEPTH_WATCHLIST",
            help="The list of things to keep collecting; see deploy/watchlist.example.txt",
        ),
    ],
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
    every: Annotated[
        float,
        typer.Option(
            "--every",
            envvar="TUBEDEPTH_WATCH_SECONDS",
            help=(
                "Stay up and queue the list again this often, in seconds. 0 queues once and exits."
            ),
        ),
    ] = 0.0,
) -> None:
    """Queue a whole watch list, forced, once or on an interval.

    The list is typed — `video`, `channel`, `search`, `playlist`, `trending`,
    one directive per line — which is what lets one schedule collect by
    channel, by trend keyword and by region at the same time. A listing line
    carries `--then video.metadata`, so it fans out to a job per video it
    finds: one such line is up to `TUBEDEPTH_LISTING_LIMIT` collections, not
    one. The `+comments` variants (`channel+comments`, `search+comments`,
    `playlist+comments`) fan out to a comment harvest per video as well — the
    most expensive kind in the system, opted into line by line.
    `deploy/watchlist.example.txt` has the arithmetic.

    The list is a required argument rather than an option with a default,
    because the failure worth designing against is a watcher that quietly
    watches nothing.

    Without `--every` this queues once and exits, which is what a timer wants
    and how `deploy/tubedepth-watch.timer` runs it — systemd has a scheduler
    already and a resident process would be a second, worse one. `--every` is
    for the environments that do not, compose among them.

    **A first pass that cannot read its list fails; a later one is logged and
    skipped.** The two are different situations. At startup an unreadable list
    is a misconfiguration, and exiting non-zero is how the operator finds out
    at the moment they are watching. Once resident, the same failure is almost
    always a half-finished edit — and killing the watcher over it would stop
    collection until somebody noticed, which is the outcome this command
    exists to prevent. Logged loudly every interval, with nothing queued from a
    list that did not parse, the operator sees it and the watcher survives it.
    """
    configure_logging()
    registry = default_registry()
    database = _database(data_directory)

    def sweep() -> None:
        queued = _watch_pass(database, registry, watchlist)
        typer.echo(f"✓ {queued} job(s) queued from {watchlist}")

    if every <= 0:
        sweep()
        return

    stop = _stopping_on_signals()
    sweep()
    # `stop.wait` rather than `time.sleep`, the same choice and the same reason
    # as `Worker._wait`: the unit sends SIGINT to stop this, and a sleeping
    # process would still shut down eventually while costing the full interval
    # to do it — which an operator watching `systemctl stop` cannot tell from a
    # hang.
    while not stop.wait(every):
        try:
            sweep()
        except Exception:
            # Deliberately everything, matching `Worker.serve`. See the
            # docstring: after the first pass, staying up and complaining
            # beats exiting and collecting nothing.
            logger.exception("a watch pass failed; the list is read again on the next one")


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
    # Sized to `--concurrency`, not the API's fixed default: `Worker.drain`
    # runs one claim thread and one lease-renewal thread per unit of
    # concurrency (`worker.py`'s `pump` and `_holding_lease`), both against
    # the write engine, so a fixed pool of 4 starves at concurrency > 2 —
    # measured directly (`docs/status.md`), not assumed from the thread
    # count. `service-db.json` carries this term in the connection budget.
    worker = Worker(
        database=_database(data_directory, pool_size=concurrency, max_overflow=concurrency),
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
        logger.info("stopping after the current pass; signal again to stop now")
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

    Nothing else changes a schema. `create_schema` no longer runs on the boot
    path: a boot that adds a column leaves `alembic_version` untouched, and the
    next upgrade then tries to create what is already there.
    """
    from alembic import command
    from alembic.config import Config

    data_directory.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parent.parent.parent
    configuration = Config(str(root / "alembic.ini"))
    configuration.set_main_option("script_location", str(root / "migrations"))
    url = database_url()

    # Not passed to `configuration` — `ConfigParser.set` validates `%`
    # interpolation syntax, and a percent-encoded password (the ordinary shape
    # of a fleet credential) makes that call raise. It would also be dead code
    # today regardless: `migrations/env.py` does not read the Config object,
    # it has its own copy of this resolver and calls it directly (Task 8
    # unifies the two, and is where a `%`-escaped version of this belongs).
    # Until then, this environment variable is the only thing that actually
    # reaches it: without it, env.py falls back to `TUBEDEPTH_DATA_DIR`
    # (default `var`), silently ignoring whatever `--data-dir` named here.
    # Restored afterwards rather than left set — this process now honours
    # `TUBEDEPTH_DATABASE_URL` everywhere (that is the point of this change),
    # so a value left behind here would silently redirect every later call in
    # the same process, `tubedepth serve`/`work` included.
    previous_url = os.environ.get("TUBEDEPTH_DATABASE_URL")
    os.environ["TUBEDEPTH_DATABASE_URL"] = url
    try:
        # Echoed masked (#30): these lines land in shell scrollback, journalctl
        # and `docker compose logs`, and the migrator credential in this URL is
        # the one that can issue DDL.
        if stamp:
            command.stamp(configuration, "head")
            typer.echo(f"✓ stamped {masked_url(url)} at the current revision")
            return
        command.upgrade(configuration, "head")
        typer.echo(f"✓ {masked_url(url)} is at the current schema")
    finally:
        if previous_url is None:
            os.environ.pop("TUBEDEPTH_DATABASE_URL", None)
        else:
            os.environ["TUBEDEPTH_DATABASE_URL"] = previous_url

    # A separate command is a command that gets skipped, and the window for
    # this one closes: attribution works by recomputing fingerprints against
    # the versions a kind has had, and retention ages out the rows it would
    # attribute. Say so here, where someone is already standing.
    #
    # `allow_sqlite_source=True` because `migrate` is the escape hatch
    # `transfer`'s preflight names (#33): bringing a pre-cutover SQLite file
    # forward is exactly what makes it transferable, and this count used to
    # construct `Database()` without the flag — so the upgrade succeeded,
    # printed its `✓`, and the command still exited non-zero. The flag is
    # inert on PostgreSQL, and every command that runs the service still
    # opens `_database()` without it.
    database = _database(data_directory, allow_sqlite_source=True)
    with database.session(readonly=True) as session:
        # tubedepth migrate runs under the migrator credential in a real
        # deployment — the same one env.py just did SET ROLE tubedepth_owner
        # with, above, for the upgrade. That role is deployment-only (rule 1)
        # and has no direct SELECT on tubedepth's tables; without becoming
        # the owner again here, this query only works by accident, when
        # --data-dir happens to resolve to a URL that is actually the
        # runtime role's rather than the migrator's.
        if database.dialect == "postgresql":
            session.execute(text("SET ROLE tubedepth_owner"))
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


@application.command(name="transfer")
def transfer_command(
    source_url: Annotated[
        str, typer.Option("--from", help="The database to carry the index out of")
    ],
    target_url: Annotated[
        str, typer.Option("--to", help="The database to carry the index into; must hold no rows")
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Count what would move and write nothing"),
    ] = False,
) -> None:
    """Carry the index between databases. Payloads stay where they are.

    The one tool #15 and #24 both assume exists for the PostgreSQL cutover:
    six tables, moved row for row with `identifier` and `fetched_at`
    preserved verbatim, rather than a `pg_dump` run by hand at 2am — see
    `tubedepth.transfer` for why that would silently corrupt every instant.

    `--from` is the one place a SQLite URL is still accepted anywhere in this
    project (`Database` otherwise refuses one since the cutover, #15): a real
    cutover's source is the SQLite file this deployment used to run on, and
    that is the whole point of the tool. `--to` is never SQLite — it is always
    refused, the same as every other database this application opens.

    `--to` names the database the connection will actually write through, so
    in a real cutover that is the runtime credential: the migrator is
    deployment-only (rule 1) and has no direct DML grant on `tubedepth`'s
    tables outside `migrations/env.py`'s `SET ROLE`.

    `--dry-run` counts each table in the source and reports it without
    opening the target for writing — an operator standing in front of a
    cutover wants to see six numbers before committing to them.
    """
    source = Database(source_url, allow_sqlite_source=True)
    # `transfer()` itself calls this on both ends (see its docstring), but
    # `--dry-run` returns before ever calling `transfer()` — without this, a
    # PostgreSQL source on the wrong `search_path` prints six confident zeros
    # (every table genuinely empty *from this connection*) instead of the
    # refusal that would tell an operator the URL is pointed at the wrong
    # place before they trust what it reports.
    source.verify_placement()
    # `transfer()` runs this preflight itself, but `--dry-run` returns before
    # ever calling it — and a pre-cutover source used to crash the rehearsal
    # with the very `no such column` the real run would die on (#33), instead
    # of the refusal naming the gap and the remedy.
    verify_source_schema(source)

    if dry_run:
        models = mapped_models()
        with source.session(readonly=True) as session:
            for table in Base.metadata.sorted_tables:
                count = session.scalar(select(func.count()).select_from(models[table.name]))
                typer.echo(f"· {table.name}: {count} row(s) would move")
        return

    target = Database(target_url)
    target.verify_placement()
    if not target.is_migrated():
        # Masked (#30): in a real cutover `--to` carries the runtime credential.
        raise ConfigurationError(f"no schema at {masked_url(target_url)} — run: tubedepth migrate")

    outcome = transfer(source=source, target=target)
    for table_name, count in outcome.rows.items():
        typer.echo(f"✓ {table_name}: {count} row(s) moved")


@application.command()
def prune(
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
    maximum_age_days: Annotated[
        int, typer.Option("--max-age-days", envvar="TUBEDEPTH_MAX_AGE_DAYS")
    ] = 30,
    maximum_gigabytes: Annotated[float, typer.Option("--max-gb", envvar="TUBEDEPTH_MAX_GB")] = 50.0,
    sweep_without_an_index: Annotated[bool, typer.Option("--sweep-without-an-index")] = False,
) -> None:
    """Remove artifacts past their retention age.

    The size limit is a backstop rather than a target: nothing here tries to
    fill it, and reaching it means the age policy is not keeping up, so it is
    reported rather than quietly absorbed by evicting whatever is nearest.

    `--sweep-without-an-index` is the operator saying that a payload store with
    no artifact rows behind it is the truth rather than a mistake. Without it
    that state is refused, because it is also what a database cutover looks
    like halfway through — and the sweep does not undo.
    """
    configure_logging()
    outcome = RetentionService(
        database=_database(data_directory),
        payloads=_payload_store(data_directory),
        policy=RetentionPolicy(
            maximum_age=timedelta(days=maximum_age_days),
            maximum_bytes=int(maximum_gigabytes * 1024**3),
            sweep_without_an_index=sweep_without_an_index,
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


@application.command()
def flatten(
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
    # `min=1` on both: a batch of 0 reads nothing for ever and a limit of 0
    # reports a clean pass over a full backlog, and both look like success.
    batch: Annotated[int, typer.Option("--batch", min=1, help="Artifacts per transaction")] = 200,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Stop after this many artifacts")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what a pass would do, write nothing")
    ] = False,
    every: Annotated[
        float,
        typer.Option(
            "--every",
            envvar="TUBEDEPTH_FLATTEN_SECONDS",
            help="Stay up and flatten again this often, in seconds. 0 runs once and exits.",
        ),
    ] = 0.0,
) -> None:
    """Unpack stored payloads into the queryable tables, incrementally.

    The artifact index keeps observations as blobs on disk, which PostgREST
    cannot see into; this walks everything new since the last pass and
    upserts the flattened rows. Idempotent by construction — the tables'
    keys make a replayed artifact a no-op — so rerunning after a crash is
    the recovery procedure, not a hazard.

    Without `--every` this runs one pass and exits, which is what a timer
    wants and how `deploy/tubedepth-flatten.timer` runs it. `--every` is for
    the environments with no scheduler, compose among them.
    """
    configure_logging()
    service = FlattenService(
        database=_database(data_directory), payloads=_payload_store(data_directory)
    )

    def sweep() -> None:
        outcome = service.run(batch_size=batch, limit=limit, dry_run=dry_run)
        flattened = sum(outcome.flattened.values())
        prefix = "would flatten" if dry_run else "flattened"
        typer.echo(
            f"✓ {prefix} {flattened} of {outcome.artifacts_seen} artifact(s)"
            + "".join(f"\n  {kind}: {count}" for kind, count in sorted(outcome.flattened.items()))
        )
        if outcome.skipped_unhandled:
            typer.echo(f"  skipped {outcome.skipped_unhandled} artifact(s) of unhandled kinds")
        if outcome.skipped_missing_payload:
            typer.echo(
                f"  skipped {outcome.skipped_missing_payload} artifact(s) whose payload is gone"
            )
        if outcome.errors:
            typer.echo(f"  {outcome.errors} payload(s) would not flatten — see the log", err=True)

    if every <= 0:
        sweep()
        return

    stop = _stopping_on_signals()
    sweep()
    while not stop.wait(every):
        try:
            sweep()
        except Exception:
            # Deliberately everything, matching `watch`: after the first
            # pass, staying up and complaining beats exiting and flattening
            # nothing.
            logger.exception("a flatten pass failed; retried on the next interval")


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
        # key otherwise means opening the database by hand. `created_at` is what this listing
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
