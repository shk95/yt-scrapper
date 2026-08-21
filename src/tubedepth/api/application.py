"""The FastAPI application.

Deliberately thin: every route builds a request and hands it to a service. No
business logic lives here, which is what keeps the CLI and the API from
drifting into two different answers for the same question.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator, Mapping, Sequence

# Imported for pydantic rather than for the type checker. With
# `from __future__ import annotations` every annotation is a string, and a
# response model naming a type this module has not imported fails at request
# time with "is not fully defined" — the second trap that future-annotations
# has set in this file.
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, Security, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .. import __version__
from ..collection import CollectionService
from ..database import Database
from ..errors import (
    ConfigurationError,
    ConflictError,
    ExtractionError,
    NotFoundError,
    RateLimitedError,
    RetractedError,
    TubedepthError,
    UnauthenticatedError,
    UnavailableError,
    UpstreamError,
    ValidationError,
)
from ..health import SourceHealthService
from ..identifiers import normalize_target
from ..models import WORKER_CONTROL_ID, Artifact, Job, LaneHealth, WorkerControl, utcnow
from ..payload_store import PayloadStore
from ..repositories import JobRepository, JobState
from ..services.keys import ApiKeyService, VerifiedKey
from ..settings import api_key_required
from ..sources import SourceRegistry, default_registry
from ..sources.registry import DataSource, attempts_for, retracted_versions_of

logger = logging.getLogger(__name__)

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)

# First match wins, so every subclass precedes its base and TubedepthError is
# last. A new error added above its base is a one-line change; added below it,
# it silently becomes a 500.
STATUS_BY_ERROR: tuple[tuple[type[TubedepthError], int, str], ...] = (
    (UnauthenticatedError, status.HTTP_401_UNAUTHORIZED, "unauthenticated"),
    (RateLimitedError, status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited"),
    # 422 as a literal: Starlette renamed the constant and referencing the old
    # name prints a deprecation warning on every import — which lands in the
    # middle of `tubedepth key create` output, where the one line that matters
    # is a secret shown once.
    (ValidationError, 422, "invalid_request"),
    (NotFoundError, status.HTTP_404_NOT_FOUND, "not_found"),
    # 404 rather than the catch-all 500: a private, deleted, members-only or
    # region-blocked video is a fact about the video, not a bug in this
    # service, and answering `internal_error` sends an operator into our
    # tracebacks for something no code change here fixes. Kept apart from
    # `not_found` because "you may not see this" and "there is no such thing"
    # are different answers to give a client.
    (UnavailableError, status.HTTP_404_NOT_FOUND, "unavailable"),
    (ConflictError, status.HTTP_409_CONFLICT, "conflict"),
    # 410 rather than 404: the observation happened and was withdrawn, and a
    # 404 would tell a reader building a history that it never happened.
    (RetractedError, status.HTTP_410_GONE, "retracted"),
    # 502 rather than 500: the upstream answered, our parser did not understand
    # it. A 500 sends an operator into our tracebacks; a 502 sends them to the
    # renderer names in the message.
    (ExtractionError, status.HTTP_502_BAD_GATEWAY, "parse_mismatch"),
    (UpstreamError, status.HTTP_502_BAD_GATEWAY, "upstream_error"),
    # 503 rather than the catch-all 500: our own misconfiguration is not a bug
    # in the code, and "fix the configuration and retry" is the whole of the
    # instruction. Since #16 a `search_path` that does not lead with this
    # project's schema raises this from `Database` itself, so the most likely
    # cause of a 503 here is a role whose search_path was never set — an
    # operator reading `internal_error` would go looking for our traceback
    # instead. An unset `TUBEDEPTH_DATA_API_KEY` is the same shape of answer.
    (ConfigurationError, status.HTTP_503_SERVICE_UNAVAILABLE, "not_configured"),
    (TubedepthError, status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error"),
)


def _describe_rejected_request(reported: Sequence[Any]) -> str:
    """FastAPI's `errors()` list rendered as one line a person can read.

    Each entry is a dict carrying a `loc` tuple, a message, and the offending
    input. Passing the structure through would put pydantic's internal error
    types and a copy of the request body into a field this API documents as
    written for a person; naming the place and the reason is the part a caller
    can act on.
    """
    lines: list[str] = []
    for detail in reported:
        if not isinstance(detail, Mapping):
            lines.append(str(detail))
            continue
        where = ".".join(str(part) for part in detail.get("loc", ()))
        reason = str(detail.get("msg", "is not valid"))
        lines.append(f"{where}: {reason}" if where else reason)
    return "; ".join(lines) or "the request could not be understood"


def _aware_time_bound(name: str, value: datetime | None) -> datetime | None:
    """A `since`/`until` bound, refused at the boundary unless it carries an offset.

    `?since=2026-08-21` — the most natural thing to type — parses to a *naive*
    datetime, which FastAPI accepts and the comparison against a `UtcDateTime`
    column then refuses deep in the driver: a ValueError that is neither a
    `TubedepthError` nor a `RequestValidationError`, so a pure read answered
    500 `internal_error` with a message about refusing to *store*. Guessing a
    timezone here would be worse — a bound eight hours off silently returns
    the wrong rows — so the only honest answer is 422, naming what to send.
    """
    if value is not None and value.tzinfo is None:
        raise ValidationError(
            f"{name} has no timezone offset — send an RFC 3339 timestamp with one, "
            f"such as 2026-08-21T00:00:00Z"
        )
    return value


class JobSubmission(BaseModel):
    kind: str
    target: str
    refresh: bool = False
    # Per request rather than per deployment: the submitter knows where its
    # answer should go, and different clients of one instance want different
    # places. Validated as a URL here because a malformed one stored is a
    # delivery that fails on every sweep forever.
    webhook_url: HttpUrl | None = None


class JobView(BaseModel):
    job_id: str
    kind: str
    target: str
    state: str
    attempt_count: int
    error_code: str | None = None
    error_message: str | None = None
    # Which key submitted this, and which worker holds it. Both were written on
    # every job and readable from nowhere, so "identify the runaway client" and
    # "which worker is stuck on this" meant opening the database by hand.
    api_key_id: str | None = None
    claimed_by: str | None = None
    payload_bytes: int | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None


MAXIMUM_BATCH = 500


class BatchSubmission(BaseModel):
    kind: str
    targets: list[str]
    refresh: bool = False
    webhook_url: HttpUrl | None = None


class HeldView(BaseModel):
    """A target the batch did not have to queue, and where its answer is."""

    target: str
    digest: str


class BatchView(BaseModel):
    queued: list[JobView]
    # Reported rather than returned. A hundred cached payloads is megabytes,
    # and the caller asked to collect these, not to download them — the digest
    # is what `GET /v1/artifacts/{digest}` needs to hand any of them over.
    held: list[HeldView]


class SourceHealthView(BaseModel):
    """One source's recent behaviour, as an operator needs to read it.

    `status` distinguishes causes that need different fixes: `broken` is our
    parser, `blocked` is the address, `degraded` is one bad call, `stale` is a
    source nothing has exercised lately, and `unknown` is one never tried. A
    dashboard showing green for something nobody has run is worse than one
    admitting it does not know.
    """

    kind: str
    status: str
    consecutive_failures: int
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error_code: str | None = None
    # The actionable half. The code says what kind of failure; the message
    # names the renderer that changed, which is what a `broken` source needs
    # someone to look at. It was recorded from the day this table existed and
    # read by nothing.
    last_error_message: str | None = None


class JobListView(BaseModel):
    """A page of jobs, newest first, with the cursor for the next one.

    Paged rather than complete because the caller is a browser and the table
    grows without bound. `cursor` is null when the page is the last one, so a
    client stops by reading the response rather than by counting.
    """

    jobs: list[JobView]
    cursor: str | None = None


class ArtifactView(BaseModel):
    kind: str
    target: str
    # Which normalizer wrote these bytes. Null for anything collected before
    # the column existed; the fingerprint holds the version and is a SHA-256,
    # so it cannot be recovered from the row itself.
    schema_version: str | None = None
    digest: str
    byte_count: int
    fetched_at: datetime
    fresh_until: datetime


class ArtifactPayloadView(BaseModel):
    """One stored observation, verbatim, with what a reader needs to interpret it.

    The bytes are returned exactly as they were collected — no model is in this
    path, so an old payload the current normalizer could not parse still comes
    back rather than raising. That is the whole point of a history route: the
    thing worth keeping is the original observation.
    """

    digest: str
    kind: str
    target: str
    # How many observations recorded these exact bytes, and when the first of
    # them was. A digest is not one observation: the store is content-addressed,
    # so a video whose counts have not moved records a new row against the same
    # digest — half the rows in a store a watch list has been running against.
    # Answering with only the newest `fetched_at` misdates every older
    # duplicate, and the span is the more useful fact anyway: nothing changed
    # between these two times.
    observations: int
    first_fetched_at: datetime | None
    fetched_at: datetime
    schema_version: str | None
    current_schema_version: str | None
    # Computed from the bytes and from the model, rather than declared. A field
    # the older version never collected is simply absent here, which is a
    # stronger and truer statement than a null — and a hand-maintained list of
    # "what v1 lacked" would drift against data nobody can re-derive.
    payload_fields: list[str]
    current_fields: list[str]
    payload: Any


class ArtifactListView(BaseModel):
    artifacts: list[ArtifactView]
    cursor: str | None = None


class LaneHealthView(BaseModel):
    """What the rate controller currently allows on one route.

    `window` is a measured ceiling rather than a setting — it halves when the
    upstream refuses and grows back — so a window well under one is the number
    that explains why a queue is draining slowly.
    """

    egress: str
    lane: str
    window: float
    in_flight: int
    quarantine_streak: int
    # Null when the lane is open. Present means nothing on this route will be
    # attempted until then, which from outside is indistinguishable from an
    # empty queue unless something says so.
    quarantined_until: datetime | None = None
    observed_at: datetime | None = None


class ControlView(BaseModel):
    paused: bool
    reason: str | None = None
    changed_at: datetime | None = None


class ControlChange(BaseModel):
    paused: bool
    # Optional, and worth filling in. A pause nobody can explain an hour later
    # is a pause nobody dares lift.
    reason: str | None = None


class HealthView(BaseModel):
    status: str
    version: str
    queued: int = Field(default=0)
    running: int = Field(default=0)
    sources: list[SourceHealthView] = Field(default_factory=list)
    lanes: list[LaneHealthView] = Field(default_factory=list)


# Dependencies live at module level, not inside the factory.
#
# This module uses `from __future__ import annotations`, as every module here
# does, so FastAPI resolves annotations from the module namespace after the
# fact. A dependency defined inside the factory is a local name that resolution
# cannot see, and `Annotated[Session, Depends(get_session)]` silently degrades into
# a required query parameter — every route then answers 422 for a missing
# argument nobody wrote. Reading collaborators off app.state avoids the closure
# entirely.


DASHBOARD = Path(__file__).parent / "dashboard.html"

# A page big enough to fill a screen and small enough that a careless caller
# cannot ask for the whole table.
MAXIMUM_PAGE = 500


def _encode_cursor(moment: datetime, identifier: str) -> str:
    """Opaque, and base64url because it travels in a query string.

    An ISO timestamp carries a `+` for its offset, which a URL reads as a
    space — so the plain form worked in a test client and broke against a real
    one. Opaque also keeps clients from building their own, which would freeze
    the ordering columns into the public contract.
    """
    raw = f"{moment.isoformat()}|{identifier}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        moment, _, identifier = base64.urlsafe_b64decode(padded).decode().partition("|")
        return datetime.fromisoformat(moment), identifier
    except (ValueError, UnicodeDecodeError) as error:
        raise ValidationError(f"cursor is not one this API issued: {cursor}") from error


def _job_view(job: Job) -> JobView:
    return JobView(
        job_id=job.identifier,
        kind=job.kind,
        target=job.target,
        state=job.state.value,
        attempt_count=job.attempt_count,
        error_code=job.error_code,
        error_message=job.error_message,
        api_key_id=job.api_key_id,
        claimed_by=job.claimed_by,
        payload_bytes=job.payload_bytes,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


def _current_source(registry: SourceRegistry, kind: str) -> DataSource | None:
    """The source behind a kind, or None for a retired kind.

    A retired kind keeps its history: it has no source to ask, so nothing is
    retracted and nothing is claimed about the current shape.
    """
    try:
        return registry.get(kind)
    except TubedepthError:
        return None


def _refuse_withdrawn_payload(
    source: DataSource | None,
    kind: str,
    schema_version: str | None,
    *,
    subject: str,
    remedy: str,
) -> None:
    """The retraction gate, shared by every route that hands stored bytes out.

    It first lived inline in `GET /v1/artifacts/{digest}`, and the identical
    bytes walked out through `GET /v1/jobs/{job_id}/result` with a 200 (#34) —
    a retraction that refuses only one of two doors protects only the readers
    who happen to arrive holding a digest.

    `subject` names the payload in the terms the caller is actually holding —
    a digest on the artifact route, a job id on the result route — and
    `remedy` finishes the 409 from the same standpoint.
    """
    retracted = retracted_versions_of(source) if source is not None else frozenset()
    if retracted and schema_version is None:
        # Not "fine", "not known". A kind that has withdrawn a version has
        # rows that predate the column holding NULL, and reading NULL as safe
        # is how a known-bad observation gets served with a 200 — the exact
        # thing the withdrawal exists to refuse. 409 rather than 410, because
        # we are not claiming it *is* retracted: we are saying the question
        # cannot be answered until something answers it.
        raise ConflictError(
            f"the schema version of {subject} was never recorded, and {kind} has "
            f"withdrawn a version — {remedy}"
        )
    if schema_version in retracted:
        raise RetractedError(
            f"{subject} was collected by schema version {schema_version}, which is "
            "retracted: its payloads are wrong rather than merely old"
        )


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_payloads(request: Request) -> PayloadStore:
    return request.app.state.payloads


def get_registry(request: Request) -> SourceRegistry:
    return request.app.state.registry


def get_reading_session(request: Request) -> Iterator[Session]:
    """For routes that only read, which is most of them.

    A read-only session takes no write lock, so it does not queue behind the
    worker. The default session does — see `Database.session` — and using it
    for a route that counts rows was measurably expensive: p99 1,434 ms against
    335 ms for a route that touches no database at all, under the same load.
    """
    with request.app.state.database.session(readonly=True) as opened:
        yield opened


def get_session(request: Request) -> Iterator[Session]:
    """For routes that write. Takes the write lock on its first statement."""
    with request.app.state.database.session() as opened:
        yield opened


def require_api_key(
    request: Request,
    presented: Annotated[str | None, Security(API_KEY_HEADER)],
) -> VerifiedKey | None:
    """The key, or None when this deployment does not ask for one.

    None rather than a stand-in `VerifiedKey`: a job submitted with no key has
    no key to attribute it to, and inventing an identifier for it would put a
    row in `jobs.api_key_id` that names nothing in `api_keys`.

    A key presented to an instance that does not require one is still verified,
    so a caller that keeps sending its header keeps its attribution and its
    allowance, and a wrong key still fails loudly rather than being ignored.
    """
    if not request.app.state.require_api_key and not presented:
        return None
    if not presented:
        raise UnauthenticatedError("api key missing or not recognised")
    return ApiKeyService(request.app.state.database).verify(presented)


def create_application(
    *,
    database: Database,
    payloads: PayloadStore,
    registry: SourceRegistry | None = None,
    require_key: bool | None = None,
) -> FastAPI:
    registry = registry or default_registry()
    # Resolved once, here, rather than read per request: an operator who
    # changes the variable expects a restart to be what applies it, and a
    # dependency that re-read the environment would make the same instance
    # answer 401 and 202 to the same request depending on when it arrived.
    require_key = api_key_required() if require_key is None else require_key

    application = FastAPI(
        title="tubedepth",
        version=__version__,
        summary="YouTube data the official Data API does not expose.",
    )

    application.state.database = database
    application.state.payloads = payloads
    application.state.registry = registry
    application.state.require_api_key = require_key

    @application.exception_handler(TubedepthError)
    async def handle_domain_error(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, TubedepthError)
        code, label = next(
            (
                (mapped_status, mapped_label)
                for error_type, mapped_status, mapped_label in STATUS_BY_ERROR
                if isinstance(error, error_type)
            ),
            (status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error"),
        )
        logger.warning("%s %s -> %s: %s", request.method, request.url.path, label, error)
        return JSONResponse(
            status_code=code,
            content={"error": {"code": label, "message": str(error)}},
        )

    @application.exception_handler(RequestValidationError)
    async def handle_rejected_request(request: Request, error: Exception) -> JSONResponse:
        """The framework's own 422, in the shape the reference promises.

        Everything this code raises reaches the handler above and comes out as
        `{"error": {"code": ..., "message": ...}}`. Everything FastAPI rejects
        before a handler runs — a malformed body, an unparseable `since=`, a
        `limit` outside its bounds — came out as `{"detail": [...]}` instead,
        so a client needed two branches for one class of failure and the
        reference's "every error is the same shape" was false for the errors a
        client is most likely to provoke.

        `invalid_request` rather than a new label: it is what `ValidationError`
        already maps to above, and a cursor this API did not issue — the same
        failure, caught one layer deeper — has always answered with it.
        """
        assert isinstance(error, RequestValidationError)
        message = _describe_rejected_request(error.errors())
        logger.warning("%s %s -> invalid_request: %s", request.method, request.url.path, message)
        # 422 as a literal for the reason STATUS_BY_ERROR gives above.
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "invalid_request", "message": message}},
        )

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> HTMLResponse:
        """The operator page: an empty shell that calls the same `/v1` routes.

        Unauthenticated because it carries no data. It asks the browser for a
        key and sends it as a header, which is the only place this project's
        auth lives — putting the requirement on the HTML would mean a secret in
        a URL or a cookie instead.

        Self-contained by rule, asserted by a test. A private tool on a private
        network cannot assume the internet is reachable, and an external
        stylesheet would tell a third party when this instance is being looked
        at.
        """
        return HTMLResponse(DASHBOARD.read_text(encoding="utf-8"))

    # Unauthenticated on purpose: something has to be reachable before you have
    # a key, or a broken deploy cannot be diagnosed from outside.
    @application.get("/healthz", response_model=HealthView)
    def healthz(
        open_session: Annotated[Session, Depends(get_reading_session)],
        database: Annotated[Database, Depends(get_database)],
    ) -> HealthView:
        counts = {
            state: open_session.query(Job).filter(Job.state == state).count()
            for state in (JobState.QUEUED, JobState.RUNNING)
        }
        # `status` stays "ok" while individual sources are not. This endpoint is
        # read by things that restart processes, and one broken parser is not a
        # reason to cycle an API whose other nine kinds are still collecting.
        # The bad news travels in the detail, where a person reads it.
        lanes = open_session.scalars(
            select(LaneHealth).order_by(LaneHealth.egress, LaneHealth.lane)
        ).all()
        return HealthView(
            status="ok",
            version=__version__,
            queued=counts[JobState.QUEUED],
            running=counts[JobState.RUNNING],
            lanes=[
                LaneHealthView(
                    egress=row.egress,
                    lane=row.lane,
                    window=row.window,
                    in_flight=row.in_flight,
                    quarantine_streak=row.quarantine_streak,
                    quarantined_until=row.quarantined_until,
                    observed_at=row.observed_at,
                )
                for row in lanes
            ],
            sources=[
                SourceHealthView(
                    kind=entry.kind,
                    status=entry.status,
                    consecutive_failures=entry.consecutive_failures,
                    last_success_at=entry.last_success_at,
                    last_failure_at=entry.last_failure_at,
                    last_error_code=entry.last_error_code,
                    last_error_message=entry.last_error_message,
                )
                for entry in SourceHealthService(database=database).snapshot().values()
            ],
        )

    # The dependency sits on the router rather than on each handler, so a route
    # added later is protected by construction and a forgotten decorator cannot
    # open a hole. A test walks the routes and asserts it.
    versioned = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])

    @versioned.get("/control", response_model=ControlView)
    def read_control(
        open_session: Annotated[Session, Depends(get_reading_session)],
    ) -> ControlView:
        """Whether the worker has been told to stop claiming.

        No row means nobody has ever paused this, which is not an error and is
        reported as running.
        """
        control = open_session.get(WorkerControl, WORKER_CONTROL_ID)
        if control is None:
            return ControlView(paused=False)
        return ControlView(
            paused=control.paused, reason=control.reason, changed_at=control.changed_at
        )

    @versioned.patch("/control", response_model=ControlView)
    def change_control(
        change: ControlChange,
        open_session: Annotated[Session, Depends(get_session)],
    ) -> ControlView:
        """Pause or resume the worker.

        The API and the worker are separate processes on purpose, so this
        cannot reach in and stop anything. It writes the row the worker reads
        at the top of each drain, and `tubedepth work` drains and exits with
        the unit restarting it every ten seconds — so a pause takes effect
        within about that, and a job already running finishes.

        Paused means claim nothing. Queued jobs stay queued and nothing is
        failed on the way in, so resuming is the whole of the undo.
        """
        control = open_session.get(WorkerControl, WORKER_CONTROL_ID) or WorkerControl(
            identifier=WORKER_CONTROL_ID
        )
        control.paused = change.paused
        control.reason = change.reason
        control.changed_at = utcnow()
        open_session.add(control)
        open_session.flush()
        return ControlView(
            paused=control.paused, reason=control.reason, changed_at=control.changed_at
        )

    @versioned.get("/sources")
    def list_sources(
        registry: Annotated[SourceRegistry, Depends(get_registry)],
    ) -> dict[str, Any]:
        """What this build can collect. Read from the registry, so a new source
        documents itself."""
        return dict(registry.describe())

    @versioned.post("/jobs", response_model=None)
    def submit_job(
        submission: JobSubmission,
        response: Response,
        open_session: Annotated[Session, Depends(get_session)],
        api_key: Annotated[VerifiedKey | None, Depends(require_api_key)],
        registry: Annotated[SourceRegistry, Depends(get_registry)],
        payloads: Annotated[PayloadStore, Depends(get_payloads)],
        database: Annotated[Database, Depends(get_database)],
    ) -> Any:
        source = registry.get(submission.kind)
        target = normalize_target(source.target_type, submission.target)

        if not submission.refresh:
            collection = CollectionService(payloads=payloads, database=database, registry=registry)
            cached = collection.cached(submission.kind, target)
            if cached is not None:
                # A fresh answer should not cost a poll cycle.
                response.status_code = status.HTTP_200_OK
                return PlainTextResponse(
                    payloads.read(cached.payload.digest).decode(),
                    media_type="application/json",
                )

        job = Job(
            kind=submission.kind,
            target=target,
            api_key_id=api_key.identifier if api_key else None,
            refresh=submission.refresh,
            # How many tries a kind is worth is a property of what collecting
            # it costs, which the registry knows and a submitter does not — so
            # it is not a field on the submission.
            max_attempts=attempts_for(source),
            webhook_url=str(submission.webhook_url) if submission.webhook_url else None,
        )
        open_session.add(job)
        open_session.flush()
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Location"] = f"/v1/jobs/{job.identifier}"
        return JobView(
            job_id=job.identifier,
            kind=job.kind,
            target=job.target,
            state=job.state.value,
            attempt_count=job.attempt_count,
        )

    @versioned.post("/jobs/batch", response_model=BatchView, status_code=202)
    def submit_batch(
        submission: BatchSubmission,
        open_session: Annotated[Session, Depends(get_session)],
        api_key: Annotated[VerifiedKey | None, Depends(require_api_key)],
        registry: Annotated[SourceRegistry, Depends(get_registry)],
        payloads: Annotated[PayloadStore, Depends(get_payloads)],
        database: Annotated[Database, Depends(get_database)],
    ) -> BatchView:
        """Queue one kind for many targets, at the cost of one request.

        A key is allowed sixty requests a minute, so a hundred-video sweep
        submitted one at a time is rate-limited before it is half done. That is
        the difference between an API that can express a sweep and one that can
        run it.

        **All or nothing.** Every target is normalised before anything is
        queued, so one bad id refuses the batch instead of queueing the other
        ninety-nine and answering 202 — a partial sweep is the worst outcome
        here, because the caller believes it ran and the gap surfaces later as
        an absence nobody is looking for.

        Unlike `POST /v1/jobs` this never returns a payload. A target already
        held is named with its digest, which is what
        `GET /v1/artifacts/{digest}` needs; returning a hundred bodies would
        make a submission a bulk download.
        """
        source = registry.get(submission.kind)
        if not submission.targets:
            raise ValidationError("a batch names at least one target")
        if len(submission.targets) > MAXIMUM_BATCH:
            raise ValidationError(
                f"a batch is at most {MAXIMUM_BATCH} targets, and this one names "
                f"{len(submission.targets)}"
            )
        # Normalised first, all of them, before a single row is added.
        targets = [normalize_target(source.target_type, target) for target in submission.targets]

        collection = CollectionService(payloads=payloads, database=database, registry=registry)
        queued: list[JobView] = []
        held: list[HeldView] = []
        for target in targets:
            if not submission.refresh:
                cached = collection.cached(submission.kind, target)
                if cached is not None:
                    held.append(HeldView(target=target, digest=cached.payload.digest))
                    continue
            job = Job(
                kind=submission.kind,
                target=target,
                api_key_id=api_key.identifier if api_key else None,
                refresh=submission.refresh,
                max_attempts=attempts_for(source),
                webhook_url=str(submission.webhook_url) if submission.webhook_url else None,
            )
            open_session.add(job)
            open_session.flush()
            queued.append(
                JobView(
                    job_id=job.identifier,
                    kind=job.kind,
                    target=job.target,
                    state=job.state.value,
                    attempt_count=job.attempt_count,
                )
            )
        return BatchView(queued=queued, held=held)

    @versioned.get("/jobs", response_model=JobListView)
    def list_jobs(
        open_session: Annotated[Session, Depends(get_reading_session)],
        state: str | None = None,
        kind: str | None = None,
        target: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        # Declared rather than clamped. A silent clamp turns `limit=100000`
        # into 500 and `limit=0` into 1 without saying so, and a bare
        # `limit: int` advertises an unbounded integer in the OpenAPI
        # document — so the schema promised something the code refused to do.
        # MAXIMUM_PAGE is module-level, so it is the one number here too.
        limit: int = Query(default=50, ge=1, le=MAXIMUM_PAGE),
        cursor: str | None = None,
    ) -> JobListView:
        """A page of the job ledger, newest first.

        Registered before `/jobs/{job_id}` because FastAPI matches routes in
        declaration order and a literal path has to win over a parameterised
        one that would otherwise swallow it.

        The cursor is the last row's `created_at` and identifier rather than an
        offset. An offset re-reads what it skips and drifts when rows arrive
        during paging, which on a table the worker is actively writing means
        showing the same job twice and missing another.
        """
        since = _aware_time_bound("since", since)
        until = _aware_time_bound("until", until)
        query = select(Job).order_by(Job.created_at.desc(), Job.identifier.desc())
        if state:
            query = query.where(Job.state == state)
        if kind:
            query = query.where(Job.kind == kind)
        if target:
            query = query.where(Job.target == target)
        if since:
            query = query.where(Job.created_at >= since)
        if until:
            query = query.where(Job.created_at <= until)
        if cursor:
            moment, identifier = _decode_cursor(cursor)
            # Keyset comparison spelled out rather than as a row value:
            # SQLAlchemy's typed API wants columns on both sides of the
            # comparison, not a row constructor, and the expanded form is
            # what every planner optimises regardless of dialect.
            query = query.where(
                or_(
                    Job.created_at < moment,
                    and_(Job.created_at == moment, Job.identifier < identifier),
                )
            )

        rows = list(open_session.scalars(query.limit(limit + 1)).all())
        more = rows[limit:]
        rows = rows[:limit]
        return JobListView(
            jobs=[_job_view(job) for job in rows],
            cursor=_encode_cursor(rows[-1].created_at, rows[-1].identifier) if more else None,
        )

    @versioned.get("/artifacts", response_model=ArtifactListView)
    def list_artifacts(
        open_session: Annotated[Session, Depends(get_reading_session)],
        kind: str | None = None,
        target: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = Query(default=50, ge=1, le=MAXIMUM_PAGE),
        cursor: str | None = None,
    ) -> ArtifactListView:
        """What was actually collected, as opposed to what was asked for.

        The artifact table appends rather than overwrites, so filtering by
        target gives one video's history — how its counts moved — which is the
        thing that table keeps and the job ledger cannot answer.
        """
        since = _aware_time_bound("since", since)
        until = _aware_time_bound("until", until)
        query = select(Artifact).order_by(Artifact.fetched_at.desc(), Artifact.identifier.desc())
        if kind:
            query = query.where(Artifact.kind == kind)
        if target:
            query = query.where(Artifact.target == target)
        if since:
            query = query.where(Artifact.fetched_at >= since)
        if until:
            query = query.where(Artifact.fetched_at <= until)
        if cursor:
            moment, identifier = _decode_cursor(cursor)
            query = query.where(
                or_(
                    Artifact.fetched_at < moment,
                    and_(Artifact.fetched_at == moment, Artifact.identifier < identifier),
                )
            )

        rows = list(open_session.scalars(query.limit(limit + 1)).all())
        more = rows[limit:]
        rows = rows[:limit]
        return ArtifactListView(
            artifacts=[
                ArtifactView(
                    kind=artifact.kind,
                    target=artifact.target,
                    schema_version=artifact.schema_version,
                    digest=artifact.digest,
                    byte_count=artifact.byte_count,
                    fetched_at=artifact.fetched_at,
                    fresh_until=artifact.fresh_until,
                )
                for artifact in rows
            ],
            cursor=_encode_cursor(rows[-1].fetched_at, rows[-1].identifier) if more else None,
        )

    @versioned.get("/artifacts/{digest}", response_model=ArtifactPayloadView)
    def read_artifact(
        digest: str,
        open_session: Annotated[Session, Depends(get_reading_session)],
        payloads: Annotated[PayloadStore, Depends(get_payloads)],
        registry: Annotated[SourceRegistry, Depends(get_registry)],
    ) -> ArtifactPayloadView:
        """One observation from the history, addressed by its content.

        `GET /v1/artifacts` has always handed out digests and nothing could
        dereference them — reaching an old payload meant having kept the job id
        that produced it, and retention deletes artifacts without touching job
        rows, so those two age apart.

        The bytes come back verbatim. No model is in this path, deliberately:
        a payload written by an older normalizer that the current one would
        reject still reads, because the original observation is the thing worth
        keeping and re-parsing it with today's shape is how history gets lost.
        """
        observed = list(
            open_session.scalars(
                select(Artifact)
                .where(Artifact.digest == digest)
                .order_by(Artifact.fetched_at.desc())
            ).all()
        )
        artifact = observed[0] if observed else None
        if artifact is None:
            raise NotFoundError(f"no artifact stored with digest: {digest}")

        source = _current_source(registry, artifact.kind)
        _refuse_withdrawn_payload(
            source,
            artifact.kind,
            artifact.schema_version,
            subject=f"the {artifact.kind} observation at {digest}",
            remedy="run `tubedepth backfill-schema-versions` and ask again",
        )

        try:
            body = json.loads(payloads.read(artifact.digest))
        except FileNotFoundError as error:
            # Two explanations, and this route can check neither: retention
            # removed the bytes, or the index and the payload store are not the
            # pair they were built as — a cutover that moved
            # TUBEDEPTH_DATABASE_URL and left TUBEDEPTH_DATA_DIR behind. It
            # used to assert the first one unconditionally, which sends whoever
            # is on call to look for a retention bug that is not there while
            # every payload in the store is unreachable for the other reason.
            raise NotFoundError(
                f"the {artifact.kind} index has a row for {digest} but the payload store has "
                f"no bytes for it — either it aged out of retention, or TUBEDEPTH_DATA_DIR is "
                f"not the store this index was built against "
                f"(observed {artifact.fetched_at:%Y-%m-%d %H:%M} UTC)"
            ) from error

        return ArtifactPayloadView(
            digest=artifact.digest,
            kind=artifact.kind,
            target=artifact.target,
            observations=len(observed),
            first_fetched_at=observed[-1].fetched_at,
            fetched_at=artifact.fetched_at,
            schema_version=artifact.schema_version,
            current_schema_version=source.schema_version if source is not None else None,
            payload_fields=sorted(body) if isinstance(body, dict) else [],
            current_fields=sorted(source.payload_model.model_fields) if source is not None else [],
            payload=body,
        )

    @versioned.get("/jobs/{job_id}", response_model=JobView)
    def read_job(
        job_id: str, open_session: Annotated[Session, Depends(get_reading_session)]
    ) -> JobView:
        job = open_session.get(Job, job_id)
        if job is None:
            raise NotFoundError(f"job not found: {job_id}")
        return _job_view(job)

    @versioned.delete("/jobs/{job_id}")
    def cancel_job(job_id: str, open_session: Annotated[Session, Depends(get_session)]) -> JobView:
        """Stop a job that is no longer wanted.

        DELETE rather than a POST to /cancel because what it removes is the
        client's claim on the work, which is the only thing here a client owns.
        The row survives — a queue that forgets what it was told to stop cannot
        answer why nothing arrived.

        What the answer means depends on the state that comes back. `cancelled`
        means it never ran. `running` means the request was recorded and the
        extraction is still going: it will not be retried and will not hand
        back a result, but it is still spending requests until it finishes.
        Saying `cancelled` in that case would announce a cost that has not
        stopped.
        """
        job = JobRepository(open_session).cancel(job_id)
        return _job_view(job)

    @versioned.get("/jobs/{job_id}/result")
    def read_result(
        job_id: str,
        open_session: Annotated[Session, Depends(get_reading_session)],
        payloads: Annotated[PayloadStore, Depends(get_payloads)],
        registry: Annotated[SourceRegistry, Depends(get_registry)],
    ) -> PlainTextResponse:
        job = open_session.get(Job, job_id)
        if job is None:
            raise NotFoundError(f"job not found: {job_id}")
        if job.payload_digest is None:
            # 409 rather than 404: the job exists, it has not finished. Telling
            # those apart is the difference between "wait" and "you asked for
            # something that does not exist".
            raise ConflictError(f"job has not finished: {job_id} is {job.state.value}")
        try:
            body = payloads.read(job.payload_digest).decode()
        except FileNotFoundError as error:
            # Retention deletes artifacts and never touches job rows, so this
            # is the ordinary end state of every job older than the retention
            # window rather than a corner case. Letting it raise reaches
            # FastAPI's default handler as a 500, which sends whoever is on
            # call into our tracebacks to find that retention did exactly what
            # it is configured to do. 404 is the honest answer: the job is
            # real, what it collected is not here, and it is not coming back.
            raise NotFoundError(
                f"the result of {job_id} is no longer stored: it was collected and has "
                "since aged out of retention"
            ) from error

        # The bytes the artifact route refuses must not walk out through this
        # door (#34). The job row does not carry the schema version; the
        # artifact row behind the digest does, so resolve it the way that
        # route does. After the read on purpose: a result that aged out of
        # retention keeps its honest 404 rather than gaining a complaint
        # about attributing bytes that are no longer here — and a digest
        # whose artifact rows are gone while its bytes remain reads as
        # version "not known", which is what it is.
        observed = open_session.scalars(
            select(Artifact)
            .where(Artifact.digest == job.payload_digest, Artifact.kind == job.kind)
            .order_by(Artifact.fetched_at.desc())
        ).first()
        _refuse_withdrawn_payload(
            _current_source(registry, job.kind),
            job.kind,
            observed.schema_version if observed is not None else None,
            subject=f"the result of job {job_id}",
            remedy="run `tubedepth backfill-schema-versions` and ask this job again",
        )
        return PlainTextResponse(body, media_type="application/json")

    application.include_router(versioned)
    return application
