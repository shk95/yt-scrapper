"""The FastAPI application.

Deliberately thin: every route builds a request and hands it to a service. No
business logic lives here, which is what keeps the CLI and the API from
drifting into two different answers for the same question.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

# Imported for pydantic rather than for the type checker. With
# `from __future__ import annotations` every annotation is a string, and a
# response model naming a type this module has not imported fails at request
# time with "is not fully defined" — the second trap that future-annotations
# has set in this file.
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response, Security, status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import __version__
from ..collection import CollectionService
from ..database import Database
from ..errors import (
    ConflictError,
    ExtractionError,
    NotFoundError,
    RateLimitedError,
    TubedepthError,
    UnauthenticatedError,
    UpstreamError,
    ValidationError,
)
from ..health import SourceHealthService
from ..identifiers import normalize_target
from ..models import Job
from ..payload_store import PayloadStore
from ..repositories import JobRepository, JobState
from ..services.keys import ApiKeyService, VerifiedKey
from ..sources import SourceRegistry, default_registry

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
    (ConflictError, status.HTTP_409_CONFLICT, "conflict"),
    # 502 rather than 500: the upstream answered, our parser did not understand
    # it. A 500 sends an operator into our tracebacks; a 502 sends them to the
    # renderer names in the message.
    (ExtractionError, status.HTTP_502_BAD_GATEWAY, "parse_mismatch"),
    (UpstreamError, status.HTTP_502_BAD_GATEWAY, "upstream_error"),
    (TubedepthError, status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error"),
)


class JobSubmission(BaseModel):
    kind: str
    target: str
    refresh: bool = False


class JobView(BaseModel):
    job_id: str
    kind: str
    target: str
    state: str
    attempt_count: int
    error_message: str | None = None
    payload_bytes: int | None = None


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


class HealthView(BaseModel):
    status: str
    version: str
    queued: int = Field(default=0)
    running: int = Field(default=0)
    sources: list[SourceHealthView] = Field(default_factory=list)


# Dependencies live at module level, not inside the factory.
#
# This module uses `from __future__ import annotations`, as every module here
# does, so FastAPI resolves annotations from the module namespace after the
# fact. A dependency defined inside the factory is a local name that resolution
# cannot see, and `Annotated[Session, Depends(get_session)]` silently degrades into
# a required query parameter — every route then answers 422 for a missing
# argument nobody wrote. Reading collaborators off app.state avoids the closure
# entirely.


def _job_view(job: Job) -> JobView:
    return JobView(
        job_id=job.identifier,
        kind=job.kind,
        target=job.target,
        state=job.state.value,
        attempt_count=job.attempt_count,
        error_message=job.error_message,
        payload_bytes=job.payload_bytes,
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
) -> VerifiedKey:
    if not presented:
        raise UnauthenticatedError("api key missing or not recognised")
    return ApiKeyService(request.app.state.database).verify(presented)


def create_application(
    *,
    database: Database,
    payloads: PayloadStore,
    registry: SourceRegistry | None = None,
) -> FastAPI:
    registry = registry or default_registry()

    application = FastAPI(
        title="tubedepth",
        version=__version__,
        summary="YouTube data the official Data API does not expose.",
    )

    application.state.database = database
    application.state.payloads = payloads
    application.state.registry = registry

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
        return HealthView(
            status="ok",
            version=__version__,
            queued=counts[JobState.QUEUED],
            running=counts[JobState.RUNNING],
            sources=[
                SourceHealthView(
                    kind=entry.kind,
                    status=entry.status,
                    consecutive_failures=entry.consecutive_failures,
                    last_success_at=entry.last_success_at,
                    last_failure_at=entry.last_failure_at,
                    last_error_code=entry.last_error_code,
                )
                for entry in SourceHealthService(database=database).snapshot().values()
            ],
        )

    # The dependency sits on the router rather than on each handler, so a route
    # added later is protected by construction and a forgotten decorator cannot
    # open a hole. A test walks the routes and asserts it.
    versioned = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])

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
        api_key: Annotated[VerifiedKey, Depends(require_api_key)],
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

        job = Job(kind=submission.kind, target=target, api_key_id=api_key.identifier)
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
    ) -> PlainTextResponse:
        job = open_session.get(Job, job_id)
        if job is None:
            raise NotFoundError(f"job not found: {job_id}")
        if job.payload_digest is None:
            # 409 rather than 404: the job exists, it has not finished. Telling
            # those apart is the difference between "wait" and "you asked for
            # something that does not exist".
            raise ConflictError(f"job has not finished: {job_id} is {job.state.value}")
        return PlainTextResponse(
            payloads.read(job.payload_digest).decode(), media_type="application/json"
        )

    application.include_router(versioned)
    return application
