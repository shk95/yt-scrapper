"""The worker: claim a job, run it through the registry, record what happened.

Nothing here reaches the network. The registry is injected, so the whole
claim → collect → settle path runs against a fake source in milliseconds —
which is the only way this loop gets exercised as often as it needs to be.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel

from tubedepth.database import Database
from tubedepth.egress.control import Lane, RateController
from tubedepth.egress.transport import Egress
from tubedepth.errors import NotFoundError, UpstreamError
from tubedepth.identifiers import TargetType
from tubedepth.models import Job, JobState
from tubedepth.payload_store import PayloadStore
from tubedepth.schemas import ListedVideo, VideoListing
from tubedepth.sources import SourceRegistry
from tubedepth.sources.registry import SourceCost
from tubedepth.sources.ytdlp_runtime import YtdlpRuntime
from tubedepth.worker import Worker


class EchoPayload(BaseModel):
    target: str


class EchoSource:
    kind = "video.echo"
    target_type = TargetType.VIDEO
    lane = Lane.YOUTUBE
    cost = SourceCost.STANDARD
    schema_version = "1"
    payload_model: type[BaseModel] = EchoPayload
    default_freshness = timedelta(hours=6)

    def __init__(self) -> None:
        self.calls: list[str] = []

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> EchoPayload:
        self.calls.append(target)
        return EchoPayload(target=target)


class FailingSource:
    """Fails in a way that waiting cannot fix.

    NotFoundError rather than UpstreamError on purpose: this test is about the
    reason reaching the row, and a retryable failure would requeue instead of
    finishing, which is a different behaviour covered further down.
    """

    kind = "video.failing"
    target_type = TargetType.VIDEO
    lane = Lane.YOUTUBE
    cost = SourceCost.STANDARD
    schema_version = "1"
    payload_model: type[BaseModel] = EchoPayload
    default_freshness = timedelta(hours=6)

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> EchoPayload:
        raise NotFoundError(f"nothing came back for: {target}")


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
        enqueue(database, "video.echo", f"video{index:06d}")

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
    lane = Lane.YOUTUBE
    cost = SourceCost.STANDARD
    schema_version = "1"
    payload_model: type[BaseModel] = VideoListing
    default_freshness = timedelta(hours=6)

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


class SlowSource:
    """A source that takes long enough to hold a slot.

    The cost is a constructor argument because it decides how many of these
    may run at once: an expensive kind is capped at half the workers, so a
    test asserting concurrency has to ask for a kind that is allowed it.
    """

    kind = "video.slow"
    target_type = TargetType.VIDEO
    lane = Lane.YOUTUBE
    schema_version = "1"
    payload_model: type[BaseModel] = EchoPayload
    default_freshness = timedelta(hours=6)

    def __init__(self, seconds: float = 0.3, cost: SourceCost = SourceCost.STANDARD) -> None:
        self.cost = cost
        self._seconds = seconds
        self.concurrent = 0
        self.peak = 0
        self._lock = threading.Lock()

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> EchoPayload:
        with self._lock:
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)
        try:
            time.sleep(self._seconds)
            return EchoPayload(target=target)
        finally:
            with self._lock:
                self.concurrent -= 1


def test_a_concurrent_worker_runs_several_jobs_at_once(tmp_path: Path) -> None:
    source = SlowSource(seconds=0.2)
    database, _, payloads = build(tmp_path, source)
    for index in range(6):
        enqueue(database, "video.slow", f"video{index:06d}")
    worker = Worker(
        database=database,
        registry=_registry(source),
        payloads=payloads,
        name="worker-1",
        concurrency=3,
        controller=RateController(window_ceiling=8),
    )

    completed = worker.drain()

    assert completed == 6
    assert source.peak > 1, "jobs ran one at a time; the worker is still sequential"
    # Deliberately no wall-clock assertion. An earlier version asserted the run
    # finished inside six job-durations and failed about one full-suite run in
    # four: under load it measures the test machine, not the code. The peak
    # concurrency above is direct evidence of the same claim, and the throughput
    # figures that matter are measured against real YouTube and recorded in
    # docs/status.md.


def test_the_rate_controller_caps_how_many_run_at_once(tmp_path: Path) -> None:
    """The AIMD window is a real limit, not a number kept for reporting.

    This is what connects the controller to anything: without it the worker's
    thread count is the only limit and the measured per-address tolerance is
    decoration.
    """
    source = SlowSource(seconds=0.2)
    database, _, payloads = build(tmp_path, source)
    for index in range(6):
        enqueue(database, "video.slow", f"video{index:06d}")
    controller = RateController(window_ceiling=2)
    worker = Worker(
        database=database,
        registry=_registry(source),
        payloads=payloads,
        name="worker-1",
        concurrency=6,
        controller=controller,
    )

    worker.drain()

    assert source.peak <= 2, f"the window was exceeded: {source.peak} ran at once"


def test_a_cheap_job_is_not_starved_by_a_queue_full_of_expensive_ones(
    tmp_path: Path,
) -> None:
    # The failure this prevents is the common one in a system built for
    # throughput: eight comment harvests take every slot and a sub-second
    # dislike lookup waits minutes behind them.
    slow = SlowSource(seconds=0.4, cost=SourceCost.EXPENSIVE)
    quick = EchoSource()
    database, _, payloads = build(tmp_path, slow, quick)
    for index in range(8):
        enqueue(database, "video.slow", f"slow{index:07d}")
    enqueue(database, "video.echo", "quickone123")

    worker = Worker(
        database=database,
        registry=_registry(slow, quick),
        payloads=payloads,
        name="worker-1",
        concurrency=4,
        controller=RateController(window_ceiling=8),
    )
    worker.drain()

    with database.session() as session:
        finished = session.get(Job, _identifier_of(session, "video.echo"))
        assert finished is not None
        assert finished.state is JobState.SUCCEEDED
    assert quick.calls == ["quickone123"]
    # What proves the reservation is that the expensive kind never held every
    # worker: with four threads and a half share, at most two may run at once,
    # so a slot was always free for the cheap job.
    assert slow.peak <= 2, f"expensive jobs took {slow.peak} of four slots"


def _identifier_of(session, kind: str) -> str:  # type: ignore[no-untyped-def]
    return session.query(Job).filter(Job.kind == kind).one().identifier


def _registry(*sources: object) -> SourceRegistry:
    registry = SourceRegistry()
    for source in sources:
        registry.register(source)  # type: ignore[arg-type]
    return registry


class FlakySource:
    """Fails a fixed number of times, then succeeds."""

    kind = "video.flaky"
    target_type = TargetType.VIDEO
    lane = Lane.YOUTUBE
    cost = SourceCost.STANDARD
    schema_version = "1"
    payload_model: type[BaseModel] = EchoPayload
    default_freshness = timedelta(hours=6)

    def __init__(self, failures: int) -> None:
        self._remaining = failures
        self.attempts = 0

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> EchoPayload:
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise UpstreamError(f"connection reset for: {target}")
        return EchoPayload(target=target)


class UnretryableSource:
    kind = "video.unretryable"
    target_type = TargetType.VIDEO
    lane = Lane.YOUTUBE
    cost = SourceCost.STANDARD
    schema_version = "1"
    payload_model: type[BaseModel] = EchoPayload
    default_freshness = timedelta(hours=6)

    def __init__(self) -> None:
        self.attempts = 0

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> EchoPayload:
        self.attempts += 1
        raise NotFoundError(f"no caption track for language: en ({target})")


def test_a_retryable_failure_goes_back_to_the_queue_with_a_delay(tmp_path: Path) -> None:
    source = FlakySource(failures=1)
    database, worker, _ = build(tmp_path, source)
    enqueue(database, "video.flaky", "dQw4w9WgXcQ")

    worker.run_once()

    with database.session() as session:
        job = session.query(Job).one()
        assert job.state is JobState.QUEUED
        assert job.attempt_count == 1
        assert job.scheduled_at > job.created_at, "a retry must wait before running again"


def test_a_delayed_retry_is_not_claimable_until_its_delay_has_passed(
    tmp_path: Path,
) -> None:
    # The backoff has to be enforced by the claim, not merely recorded. A
    # worker that picks the job straight back up has waited zero seconds.
    source = FlakySource(failures=1)
    database, worker, _ = build(tmp_path, source)
    enqueue(database, "video.flaky", "dQw4w9WgXcQ")

    worker.run_once()

    assert worker.run_once() is False
    assert source.attempts == 1


def test_an_unretryable_failure_is_not_tried_again(tmp_path: Path) -> None:
    source = UnretryableSource()
    database, worker, _ = build(tmp_path, source)
    enqueue(database, "video.unretryable", "dQw4w9WgXcQ")

    worker.run_once()

    with database.session() as session:
        job = session.query(Job).one()
        assert job.state is JobState.FAILED
    assert worker.run_once() is False
    assert source.attempts == 1


def test_a_job_that_exhausts_its_attempts_finally_fails(tmp_path: Path) -> None:
    source = FlakySource(failures=99)
    database, worker, _ = build(tmp_path, source)
    with database.session() as session:
        session.add(Job(kind="video.flaky", target="dQw4w9WgXcQ", max_attempts=2))

    worker.run_once()
    _clear_backoff(database)
    worker.run_once()

    with database.session() as session:
        job = session.query(Job).one()
        assert job.state is JobState.FAILED
        assert job.attempt_count == 2
        assert "connection reset" in (job.error_message or "")


def test_a_retried_job_eventually_succeeds(tmp_path: Path) -> None:
    source = FlakySource(failures=1)
    database, worker, _ = build(tmp_path, source)
    enqueue(database, "video.flaky", "dQw4w9WgXcQ")

    worker.run_once()
    _clear_backoff(database)
    worker.run_once()

    with database.session() as session:
        assert session.query(Job).one().state is JobState.SUCCEEDED
    assert source.attempts == 2


def _clear_backoff(database) -> None:  # type: ignore[no-untyped-def]
    """Bring a delayed retry forward, so the test does not wait fifteen seconds."""
    from tubedepth.models import utcnow

    with database.session() as session:
        for job in session.query(Job).filter(Job.state == JobState.QUEUED).all():
            job.scheduled_at = utcnow()


def test_the_worker_reuses_a_cached_answer_rather_than_refetching(tmp_path: Path) -> None:
    """The queue must go through the same cache the CLI does.

    Two collection paths mean one of them caches and the other does not, and
    the one that does not is the one running a hundred jobs unattended.
    """
    source = EchoSource()
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    registry = _registry(source)
    payloads = PayloadStore(tmp_path / "payloads")
    worker = Worker(
        database=database, registry=registry, payloads=payloads, name="worker-1", concurrency=1
    )
    enqueue(database, "video.echo", "dQw4w9WgXcQ")
    enqueue(database, "video.echo", "dQw4w9WgXcQ")

    worker.drain()

    assert source.calls == ["dQw4w9WgXcQ"], "the second job refetched an answer already held"


def test_a_cached_listing_still_queues_the_videos_it_holds(tmp_path: Path) -> None:
    """Re-sweeping a channel must re-check it, cheaply — not skip it.

    Suppressing fan-out on a cache hit looks like a saving and is actually a
    silent no-op: the second sweep of a channel does nothing at all. The
    follow-up jobs are what should be cheap, because each one consults the
    cache itself.
    """
    listing = ListingSource()
    echo = EchoSource()
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    worker = Worker(
        database=database,
        registry=_registry(listing, echo),
        payloads=PayloadStore(tmp_path / "payloads"),
        name="worker-1",
        concurrency=1,
    )

    with database.session() as session:
        session.add(Job(kind="channel.fake", target="@someone", follow_up_kind="video.echo"))
    worker.drain()
    first_pass = len(echo.calls)

    with database.session() as session:
        session.add(Job(kind="channel.fake", target="@someone", follow_up_kind="video.echo"))
    worker.drain()

    assert first_pass == 3
    # The listing came from cache the second time, and still produced its
    # follow-ups — which then cost nothing themselves.
    with database.session() as session:
        queued_videos = session.query(Job).filter(Job.kind == "video.echo").count()
    assert queued_videos == 6, "a cached listing produced no work at all"
    assert len(echo.calls) == 3, "the follow-ups refetched instead of hitting the cache"
