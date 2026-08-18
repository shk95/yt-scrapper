"""The FastAPI application.

Deliberately thin: every route builds a request and hands it to a service. No
business logic lives here, which is what keeps the CLI and the API from
drifting into two different answers for the same question.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
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
from ..identifiers import normalize_target
from ..models import Job, JobState
from ..payload_store import PayloadStore
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


class HealthView(BaseModel):
    status: str
    version: str
    queued: int = Field(default=0)
    running: int = Field(default=0)


# Dependencies live at module level, not inside the factory.
#
# This module uses `from __future__ import annotations`, as every module here
# does, so FastAPI resolves annotations from the module namespace after the
# fact. A dependency defined inside the factory is a local name that resolution
# cannot see, and `Annotated[Session, Depends(get_session)]` silently degrades into
# a required query parameter — every route then answers 422 for a missing
# argument nobody wrote. Reading collaborators off app.state avoids the closure
# entirely.


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_payloads(request: Request) -> PayloadStore:
    return request.app.state.payloads


def get_registry(request: Request) -> SourceRegistry:
    return request.app.state.registry


def get_session(request: Request) -> Iterator[Session]:
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
    def healthz(open_session: Annotated[Session, Depends(get_session)]) -> HealthView:
        counts = {
            state: open_session.query(Job).filter(Job.state == state).count()
            for state in (JobState.QUEUED, JobState.RUNNING)
        }
        return HealthView(
            status="ok",
            version=__version__,
            queued=counts[JobState.QUEUED],
            running=counts[JobState.RUNNING],
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
    def read_job(job_id: str, open_session: Annotated[Session, Depends(get_session)]) -> JobView:
        job = open_session.get(Job, job_id)
        if job is None:
            raise NotFoundError(f"job not found: {job_id}")
        return JobView(
            job_id=job.identifier,
            kind=job.kind,
            target=job.target,
            state=job.state.value,
            attempt_count=job.attempt_count,
            error_message=job.error_message,
            payload_bytes=job.payload_bytes,
        )

    @versioned.get("/jobs/{job_id}/result")
    def read_result(
        job_id: str,
        open_session: Annotated[Session, Depends(get_session)],
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
