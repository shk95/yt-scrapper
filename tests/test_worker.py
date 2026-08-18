"""The worker: claim a job, run it through the registry, record what happened.

Nothing here reaches the network. The registry is injected, so the whole
claim → collect → settle path runs against a fake source in milliseconds —
which is the only way this loop gets exercised as often as it needs to be.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from tubedepth.database import Database
from tubedepth.egress.transport import Egress
from tubedepth.errors import UpstreamError
from tubedepth.identifiers import TargetType
from tubedepth.models import Job, JobState
from tubedepth.payload_store import PayloadStore
from tubedepth.schemas import ListedVideo, VideoListing
from tubedepth.sources import SourceRegistry
from tubedepth.sources.ytdlp_runtime import YtdlpRuntime
from tubedepth.worker import Worker


class EchoPayload(BaseModel):
    target: str


class EchoSource:
    kind = "video.echo"
    target_type = TargetType.VIDEO

    def __init__(self) -> None:
        self.calls: list[str] = []

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> EchoPayload:
        self.calls.append(target)
        return EchoPayload(target=target)


class FailingSource:
    kind = "video.failing"
    target_type = TargetType.VIDEO

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> EchoPayload:
        raise UpstreamError(f"nothing came back for: {target}")


def build(tmp_path: Path, *sources: object) -> tuple[Database, Worker, PayloadStore]:
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    registry = SourceRegistry()
    for source in sources:
        registry.register(source)  # type: ignore[arg-type]
    payloads = PayloadStore(tmp_path / "payloads")
    worker = Worker(database=database, registry=registry, payloads=payloads, name="worker-1")
    return database, worker, payloads


def enqueue(database: Database, kind: str, target: str) -> str:
    with database.session() as session:
        job = Job(kind=kind, target=target)
        session.add(job)
        session.flush()
        return job.identifier


def test_running_one_job_collects_it_and_marks_it_succeeded(tmp_path: Path) -> None:
    source = EchoSource()
    database, worker, payloads = build(tmp_path, source)
    identifier = enqueue(database, "video.echo", "dQw4w9WgXcQ")

    assert worker.run_once() is True

    assert source.calls == ["dQw4w9WgXcQ"]
    with database.session() as session:
        job = session.get(Job, identifier)
        assert job is not None
        assert job.state is JobState.SUCCEEDED
        assert job.payload_digest is not None
        assert payloads.read(job.payload_digest)


def test_an_empty_queue_leaves_the_worker_with_nothing_to_do(tmp_path: Path) -> None:
    _, worker, _ = build(tmp_path, EchoSource())

    assert worker.run_once() is False


def test_a_failing_source_marks_the_job_failed_with_the_reason(tmp_path: Path) -> None:
    # The reason has to survive onto the row. A job that just says "failed"
    # sends whoever is on call to the logs to find out what everyone already
    # knew at the moment it happened.
    database, worker, _ = build(tmp_path, FailingSource())
    identifier = enqueue(database, "video.failing", "dQw4w9WgXcQ")

    assert worker.run_once() is True

    with database.session() as session:
        job = session.get(Job, identifier)
        assert job is not None
        assert job.state is JobState.FAILED
        assert job.error_message is not None
        assert "nothing came back" in job.error_message


def test_a_job_naming_an_unregistered_kind_fails_rather_than_hanging(
    tmp_path: Path,
) -> None:
    database, worker, _ = build(tmp_path, EchoSource())
    identifier = enqueue(database, "video.nonexistent", "dQw4w9WgXcQ")

    assert worker.run_once() is True

    with database.session() as session:
        job = session.get(Job, identifier)
        assert job is not None
        assert job.state is JobState.FAILED


def test_the_worker_drains_every_queued_job(tmp_path: Path) -> None:
    source = EchoSource()
    database, worker, _ = build(tmp_path, source)
    for index in range(5):
        enqueue(database, "video.echo", f"video{index:07d}")

    drained = worker.drain()

    assert drained == 5
    assert len(source.calls) == 5
    with database.session() as session:
        remaining = session.query(Job).filter(Job.state == JobState.QUEUED).count()
    assert remaining == 0


class ListingSource:
    """A listing source, whose result is other things to collect."""

    kind = "channel.fake"
    target_type = TargetType.CHANNEL

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> VideoListing:
        return VideoListing(
            source_kind=self.kind,
            videos=[ListedVideo(video_id=f"video{index:06d}") for index in range(3)],
        )


def test_a_listing_job_can_queue_the_videos_it_found(tmp_path: Path) -> None:
    """The link that makes large-scale collection possible.

    Without it a listing is a report someone has to read and retype. With it,
    one enqueued channel becomes a hundred queued metadata jobs and the worker
    keeps going.
    """
    database, worker, _ = build(tmp_path, ListingSource(), EchoSource())
    with database.session() as session:
        session.add(Job(kind="channel.fake", target="@someone", follow_up_kind="video.echo"))

    assert worker.run_once() is True

    with database.session() as session:
        queued = (
            session.query(Job).filter(Job.state == JobState.QUEUED, Job.kind == "video.echo").all()
        )
        assert sorted(job.target for job in queued) == [
            "video000000",
            "video000001",
            "video000002",
        ]


def test_a_listing_job_without_a_follow_up_queues_nothing(tmp_path: Path) -> None:
    # Enumerating without collecting is a legitimate thing to want — checking
    # what a channel holds should not cost a hundred extractions.
    database, worker, _ = build(tmp_path, ListingSource(), EchoSource())
    with database.session() as session:
        session.add(Job(kind="channel.fake", target="@someone"))

    worker.run_once()

    with database.session() as session:
        assert session.query(Job).filter(Job.state == JobState.QUEUED).count() == 0
