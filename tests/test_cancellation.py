"""Stopping a job that has been asked for and is no longer wanted.

Two cases, and only one of them is simple. A queued job can be cancelled by
changing a row, because nothing is happening to it. A running job cannot: a
comment harvest is inside yt-dlp, in a thread, and the only honest thing to
offer is that it will not be *retried* and its result will not be kept.

Promising more than that would be the lie worth avoiding here — a `cancelled`
state on a job whose extraction is still burning requests against the address
is worse than no cancellation at all, because it says the cost has stopped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tubedepth.database import Database
from tubedepth.egress.transport import Egress
from tubedepth.errors import ConflictError, NotFoundError
from tubedepth.models import Job, JobState
from tubedepth.repositories import JobRepository
from tubedepth.sources.ytdlp_runtime import YtdlpRuntime


def database_with(database: Database, **fields: object) -> tuple[Database, str]:
    with database.session() as session:
        job = Job(kind="video.metadata", target="video000001", **fields)
        session.add(job)
        session.flush()
        return database, job.identifier


def test_a_queued_job_is_cancelled_outright(tmp_path: Path, database: Database) -> None:
    database, identifier = database_with(database)

    with database.session() as session:
        JobRepository(session).cancel(identifier)

    with database.session() as session:
        job = session.get(Job, identifier)
        assert job is not None
        assert job.state is JobState.CANCELLED
        assert job.finished_at is not None


def test_a_running_job_is_marked_for_cancellation_but_not_declared_stopped(
    tmp_path: Path,
    database: Database,
) -> None:
    """The honest half. The extraction is inside a thread and keeps going."""
    database, identifier = database_with(database, state=JobState.RUNNING)

    with database.session() as session:
        JobRepository(session).cancel(identifier)

    with database.session() as session:
        job = session.get(Job, identifier)
        assert job is not None
        assert job.state is JobState.RUNNING, "a running job must not claim to have stopped"
        assert job.cancel_requested_at is not None


def test_a_finished_job_cannot_be_cancelled(tmp_path: Path, database: Database) -> None:
    database, identifier = database_with(
        database, state=JobState.SUCCEEDED, payload_digest="a" * 64, payload_bytes=1
    )

    with database.session() as session, pytest.raises(ConflictError, match="already finished"):
        JobRepository(session).cancel(identifier)


def test_cancelling_a_job_that_does_not_exist_is_reported(
    tmp_path: Path, database: Database
) -> None:
    database, _ = database_with(database)

    with database.session() as session, pytest.raises(NotFoundError):
        JobRepository(session).cancel("0" * 32)


def test_a_cancelled_job_is_never_claimed(tmp_path: Path, database: Database) -> None:
    """The point of the state. A cancelled job that a worker can still pick up
    has not been cancelled."""
    from datetime import timedelta

    database, identifier = database_with(database)
    with database.session() as session:
        JobRepository(session).cancel(identifier)

    with database.session() as session:
        claimed = JobRepository(session).claim(worker="worker-1", lease=timedelta(minutes=5))

    assert claimed is None


def test_a_job_cancelled_before_the_worker_reaches_it_is_not_run(
    tmp_path: Path, database: Database
) -> None:
    from test_worker import EchoSource, _registry

    from tubedepth.payload_store import PayloadStore
    from tubedepth.worker import Worker

    source = EchoSource()
    database, identifier = database_with(database)
    with database.session() as session:
        JobRepository(session).cancel(identifier)

    worker = Worker(
        database=database,
        registry=_registry(source),
        payloads=PayloadStore(tmp_path / "payloads"),
        name="worker-1",
    )
    worker.drain()

    assert source.calls == []


def test_a_running_job_cancelled_mid_flight_settles_cancelled_without_a_result(
    tmp_path: Path,
    database: Database,
) -> None:
    """The job carries no result; the cache keeps what was already fetched.

    This was written the other way round first — cancel, therefore discard —
    and that is wrong on this project's own terms. The request to YouTube had
    already gone out and been paid for by the time cancellation arrived.
    Dropping the artifact does not un-spend it; it guarantees the next caller
    spends it again, against the one budget that actually caps this system.

    So the contract is narrower and honest: the *job* is cancelled and does not
    hand back a result, and the artifact stays in a cache that is keyed by
    video rather than by who asked. Anyone needing collected data to disappear
    wants retention or access control, which are different mechanisms and are
    documented as such.
    """
    from test_worker import EchoPayload, EchoSource, _registry, enqueue

    from tubedepth.payload_store import PayloadStore
    from tubedepth.worker import Worker

    class CancellingSource(EchoSource):
        kind = "video.echo"

        def __init__(self, database: Database) -> None:
            super().__init__()
            self._database = database

        def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> EchoPayload:
            # Cancelled while this very extraction is in flight.
            with self._database.session() as session:
                for job in session.query(Job).all():
                    JobRepository(session).cancel(job.identifier)
            return super().collect(target, egress, runtime)

    identifier = enqueue(database, "video.echo", "video000001")
    source = CancellingSource(database)
    worker = Worker(
        database=database,
        registry=_registry(source),
        payloads=PayloadStore(tmp_path / "payloads"),
        name="worker-1",
    )

    worker.drain()

    with database.session() as session:
        job = session.get(Job, identifier)
        assert job is not None
        assert job.state is JobState.CANCELLED
        assert job.payload_digest is None, "a cancelled job handed back a result"
        from tubedepth.models import Artifact

        assert session.query(Artifact).count() == 1, "the request was paid for and then thrown away"


def test_a_cancelled_job_that_fails_is_not_retried(tmp_path: Path, database: Database) -> None:
    from test_worker import EchoPayload, FailingSource, _registry, enqueue

    from tubedepth.payload_store import PayloadStore
    from tubedepth.worker import Worker

    class RetryableFailure(FailingSource):
        def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> EchoPayload:
            from tubedepth.errors import UpstreamError

            raise UpstreamError(f"connection reset for: {target}")

    identifier = enqueue(database, "video.failing", "video000001")
    with database.session() as session:
        job = session.get(Job, identifier)
        assert job is not None
        job.state = JobState.RUNNING
        JobRepository(session).cancel(identifier)
        job.state = JobState.QUEUED

    worker = Worker(
        database=database,
        registry=_registry(RetryableFailure()),
        payloads=PayloadStore(tmp_path / "payloads"),
        name="worker-1",
    )
    worker.drain()

    with database.session() as session:
        job = session.get(Job, identifier)
        assert job is not None
        assert job.state is JobState.CANCELLED, "a cancelled job was requeued for another go"
