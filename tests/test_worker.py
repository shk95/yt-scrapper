"""The worker: claim a job, run it through the registry, record what happened.

Nothing here reaches the network. The registry is injected, so the whole
claim → collect → settle path runs against a fake source in milliseconds —
which is the only way this loop gets exercised as often as it needs to be.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from tubedepth.database import Database
from tubedepth.egress.control import Lane, RateController, Verdict
from tubedepth.egress.transport import Egress
from tubedepth.errors import NotFoundError, UpstreamError
from tubedepth.identifiers import TargetType
from tubedepth.models import Job, JobState, utcnow
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


def build(
    tmp_path: Path, database: Database, *sources: object
) -> tuple[Database, Worker, PayloadStore]:
    registry = SourceRegistry()
    for source in sources:
        registry.register(source)  # type: ignore[arg-type]
    payloads = PayloadStore(tmp_path / "payloads")
    worker = Worker(database=database, registry=registry, payloads=payloads, name="worker-1")
    return database, worker, payloads


def enqueue(database: Database, kind: str, target: str, *, refresh: bool = False) -> str:
    with database.session() as session:
        job = Job(kind=kind, target=target, refresh=refresh)
        session.add(job)
        session.flush()
        return job.identifier


def test_running_one_job_collects_it_and_marks_it_succeeded(
    tmp_path: Path, database: Database
) -> None:
    source = EchoSource()
    database, worker, payloads = build(tmp_path, database, source)
    identifier = enqueue(database, "video.echo", "dQw4w9WgXcQ")

    assert worker.run_once() is True

    assert source.calls == ["dQw4w9WgXcQ"]
    with database.session() as session:
        job = session.get(Job, identifier)
        assert job is not None
        assert job.state is JobState.SUCCEEDED
        assert job.payload_digest is not None
        assert payloads.read(job.payload_digest)


def test_an_empty_queue_leaves_the_worker_with_nothing_to_do(
    tmp_path: Path, database: Database
) -> None:
    _, worker, _ = build(tmp_path, database, EchoSource())

    assert worker.run_once() is False


def test_a_failing_source_marks_the_job_failed_with_the_reason(
    tmp_path: Path, database: Database
) -> None:
    # The reason has to survive onto the row. A job that just says "failed"
    # sends whoever is on call to the logs to find out what everyone already
    # knew at the moment it happened.
    database, worker, _ = build(tmp_path, database, FailingSource())
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
    database: Database,
) -> None:
    database, worker, _ = build(tmp_path, database, EchoSource())
    identifier = enqueue(database, "video.nonexistent", "dQw4w9WgXcQ")

    assert worker.run_once() is True

    with database.session() as session:
        job = session.get(Job, identifier)
        assert job is not None
        assert job.state is JobState.FAILED


def test_the_worker_drains_every_queued_job(tmp_path: Path, database: Database) -> None:
    source = EchoSource()
    database, worker, _ = build(tmp_path, database, source)
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


def test_a_listing_job_can_queue_the_videos_it_found(tmp_path: Path, database: Database) -> None:
    """The link that makes large-scale collection possible.

    Without it a listing is a report someone has to read and retype. With it,
    one enqueued channel becomes a hundred queued metadata jobs and the worker
    keeps going.
    """
    database, worker, _ = build(tmp_path, database, ListingSource(), EchoSource())
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


def test_a_listing_job_without_a_follow_up_queues_nothing(
    tmp_path: Path, database: Database
) -> None:
    # Enumerating without collecting is a legitimate thing to want — checking
    # what a channel holds should not cost a hundred extractions.
    database, worker, _ = build(tmp_path, database, ListingSource(), EchoSource())
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


def test_a_concurrent_worker_runs_several_jobs_at_once(tmp_path: Path, database: Database) -> None:
    source = SlowSource(seconds=0.2)
    database, _, payloads = build(tmp_path, database, source)
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


def test_the_rate_controller_caps_how_many_run_at_once(tmp_path: Path, database: Database) -> None:
    """The AIMD window is a real limit, not a number kept for reporting.

    This is what connects the controller to anything: without it the worker's
    thread count is the only limit and the measured per-address tolerance is
    decoration.
    """
    source = SlowSource(seconds=0.2)
    database, _, payloads = build(tmp_path, database, source)
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
    database: Database,
) -> None:
    # The failure this prevents is the common one in a system built for
    # throughput: eight comment harvests take every slot and a sub-second
    # segment lookup waits minutes behind them.
    slow = SlowSource(seconds=0.4, cost=SourceCost.EXPENSIVE)
    quick = EchoSource()
    database, _, payloads = build(tmp_path, database, slow, quick)
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


def test_disabling_the_claim_lock_lets_two_threads_exceed_the_reservation(
    tmp_path: Path,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What `Worker._claim`'s lock actually buys, forced rather than hoped for.

    The neighbouring test shows the reservation holding under real
    concurrency; it does not show *why* the lock is what makes that true
    rather than luck. This one swaps the real lock for a no-op and forces
    both worker threads past the reservation check before either registers,
    which is exactly the race `_claim`'s docstring describes. With the real
    lock this cannot happen; here, it does, every time.
    """
    import contextlib
    import itertools

    slow = SlowSource(seconds=0.2, cost=SourceCost.EXPENSIVE)
    database, _, payloads = build(tmp_path, database, slow)
    for index in range(2):
        enqueue(database, "video.slow", f"slow{index:07d}")

    worker = Worker(
        database=database,
        registry=_registry(slow),
        payloads=payloads,
        name="worker-1",
        concurrency=2,
        controller=RateController(window_ceiling=8),
    )
    # A no-op stand-in for the real lock: enters and exits without excluding
    # anyone, which is what "no lock" means for two threads racing the same
    # check-then-increment.
    monkeypatch.setattr(worker, "_lock", contextlib.nullcontext())
    # The AIMD window starts at 1.0 and only grows after a success, so with
    # only two jobs it would gate the second thread on its own, confounding
    # a test about the cost reservation specifically. Not what this test is
    # about, so it is bypassed rather than tuned around.
    monkeypatch.setattr(RateController, "acquire", lambda self, egress, lane: True)

    # Both EXPENSIVE-cost jobs are meant to be capped at one concurrent
    # (share 0.5 of concurrency=2), so the race window is: both threads read
    # the reservation as "not yet full", both proceed, both increment.
    # Rendezvous only the first call from each thread — later calls, made
    # once the queue is empty, must not block waiting for a third party.
    barrier = threading.Barrier(2, timeout=5)
    call_count = itertools.count()
    real_admissible = worker._admissible_kinds_unlocked

    def _racing_admissible_kinds_unlocked() -> list[str] | None:
        result = real_admissible()
        if next(call_count) < 2:
            barrier.wait()
        return result

    monkeypatch.setattr(worker, "_admissible_kinds_unlocked", _racing_admissible_kinds_unlocked)

    worker.drain()

    assert slow.peak > 1, (
        "the reservation held even with no lock — the forced race did not "
        "reproduce, so this test is not proving what it claims to"
    )


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


def test_a_retryable_failure_goes_back_to_the_queue_with_a_delay(
    tmp_path: Path, database: Database
) -> None:
    source = FlakySource(failures=1)
    database, worker, _ = build(tmp_path, database, source)
    enqueue(database, "video.flaky", "dQw4w9WgXcQ")

    worker.run_once()

    with database.session() as session:
        job = session.query(Job).one()
        assert job.state is JobState.QUEUED
        assert job.attempt_count == 1
        assert job.scheduled_at > job.created_at, "a retry must wait before running again"


def test_a_delayed_retry_is_not_claimable_until_its_delay_has_passed(
    tmp_path: Path,
    database: Database,
) -> None:
    # The backoff has to be enforced by the claim, not merely recorded. A
    # worker that picks the job straight back up has waited zero seconds.
    source = FlakySource(failures=1)
    database, worker, _ = build(tmp_path, database, source)
    enqueue(database, "video.flaky", "dQw4w9WgXcQ")

    worker.run_once()

    assert worker.run_once() is False
    assert source.attempts == 1


def test_an_unretryable_failure_is_not_tried_again(tmp_path: Path, database: Database) -> None:
    source = UnretryableSource()
    database, worker, _ = build(tmp_path, database, source)
    enqueue(database, "video.unretryable", "dQw4w9WgXcQ")

    worker.run_once()

    with database.session() as session:
        job = session.query(Job).one()
        assert job.state is JobState.FAILED
    assert worker.run_once() is False
    assert source.attempts == 1


def test_a_job_that_exhausts_its_attempts_finally_fails(tmp_path: Path, database: Database) -> None:
    source = FlakySource(failures=99)
    database, worker, _ = build(tmp_path, database, source)
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


def test_a_retried_job_eventually_succeeds(tmp_path: Path, database: Database) -> None:
    source = FlakySource(failures=1)
    database, worker, _ = build(tmp_path, database, source)
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


def test_the_worker_reuses_a_cached_answer_rather_than_refetching(
    tmp_path: Path, database: Database
) -> None:
    """The queue must go through the same cache the CLI does.

    Two collection paths mean one of them caches and the other does not, and
    the one that does not is the one running a hundred jobs unattended.
    """
    source = EchoSource()
    registry = _registry(source)
    payloads = PayloadStore(tmp_path / "payloads")
    worker = Worker(
        database=database, registry=registry, payloads=payloads, name="worker-1", concurrency=1
    )
    enqueue(database, "video.echo", "dQw4w9WgXcQ")
    enqueue(database, "video.echo", "dQw4w9WgXcQ")

    worker.drain()

    assert source.calls == ["dQw4w9WgXcQ"], "the second job refetched an answer already held"


def test_a_job_that_asked_for_a_refresh_collects_again(tmp_path: Path, database: Database) -> None:
    """The mirror image of the test above, and the whole point of the flag.

    Counts move, and sometimes the current number is the point. A submission
    that said so must not be handed the answer held from an hour ago — which
    means the flag has to be on the row, because the collection happens in
    another process minutes after the request that asked for it.
    """
    source = EchoSource()
    worker = Worker(
        database=database,
        registry=_registry(source),
        payloads=PayloadStore(tmp_path / "payloads"),
        name="worker-1",
        concurrency=1,
    )
    enqueue(database, "video.echo", "dQw4w9WgXcQ")
    enqueue(database, "video.echo", "dQw4w9WgXcQ", refresh=True)

    worker.drain()

    assert source.calls == ["dQw4w9WgXcQ", "dQw4w9WgXcQ"], (
        "the refresh job was served the cached answer it explicitly asked to bypass"
    )


def test_a_refresh_job_that_is_retried_still_refreshes(tmp_path: Path, database: Database) -> None:
    """A retry is the same request again, so it keeps the same intent.

    The flag lives on the row rather than in the claim, so this falls out
    rather than being arranged — the test is here because the alternative
    (deciding the bypass once, at submission) would silently stop refreshing
    the moment a job needed a second attempt.
    """
    source = FlakySource(failures=1)
    worker = Worker(
        database=database,
        registry=_registry(source),
        payloads=PayloadStore(tmp_path / "payloads"),
        name="worker-1",
        concurrency=1,
    )
    identifier = enqueue(database, "video.flaky", "dQw4w9WgXcQ", refresh=True)

    worker.drain()
    _clear_backoff(database)
    worker.drain()

    with database.session() as session:
        job = session.get(Job, identifier)
        assert job is not None
        assert job.state is JobState.SUCCEEDED
        assert job.refresh is True, "the retry lost the intent the submission recorded"


def test_a_cached_listing_still_queues_the_videos_it_holds(
    tmp_path: Path, database: Database
) -> None:
    """Re-sweeping a channel must re-check it, cheaply — not skip it.

    Suppressing fan-out on a cache hit looks like a saving and is actually a
    silent no-op: the second sweep of a channel does nothing at all. The
    follow-up jobs are what should be cheap, because each one consults the
    cache itself.
    """
    listing = ListingSource()
    echo = EchoSource()
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


def test_a_job_put_back_because_the_route_was_busy_keeps_its_attempts(
    tmp_path: Path,
    database: Database,
) -> None:
    """The attempt is counted at claim, and nothing was attempted.

    Found in a forty-job sweep at concurrency 8 against an AIMD window near 2:
    most claims lose the permit race, and each loss was burning one of three
    attempts. Jobs reached attempt 6 while still running, and a job that had
    never once reached YouTube could be failed as having "exhausted its
    attempts". The route being busy is not the job's fault and must not spend
    its budget.
    """
    source = EchoSource()
    database, _, payloads = build(tmp_path, database, source)
    identifier = enqueue(database, "video.echo", "video000001")
    quarantined = RateController(window_ceiling=1)
    quarantined.record("direct", Lane.YOUTUBE, Verdict.BLOCKED)
    worker = Worker(
        database=database,
        registry=_registry(source),
        payloads=payloads,
        name="worker-1",
        controller=quarantined,
        permit_wait=timedelta(seconds=0),
    )

    worker.run_once()

    with database.session() as session:
        job = session.get(Job, identifier)
        assert job is not None
        assert job.state is JobState.QUEUED, "the job should be back in the queue"
        assert job.attempt_count == 0, "a busy route spent one of the job's attempts"
    assert source.calls == [], "the source ran despite the route being unavailable"


def test_a_failure_that_can_never_succeed_says_so_rather_than_counting_attempts(
    tmp_path: Path,
    database: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`exhausted its attempts` reads as "we tried and gave up".

    For an error that is not retryable, that is the wrong story: no number of
    attempts would have helped, and an operator reading it goes looking for a
    flaky network instead of a video with no captions.
    """
    source = FailingSource()
    database, worker, _ = build(tmp_path, database, source)
    identifier = enqueue(database, "video.failing", "video000001")
    with database.session() as session:
        job = session.get(Job, identifier)
        assert job is not None
        job.attempt_count = job.max_attempts - 1  # the claim adds the last one

    with caplog.at_level(logging.WARNING, logger="tubedepth.worker"):
        worker.run_once()

    assert "is not retryable" in caplog.text
    assert "exhausted its attempts" not in caplog.text


def test_a_video_with_nothing_to_collect_does_not_slow_the_route_down(
    tmp_path: Path,
    database: Database,
) -> None:
    """Measured, then reproduced here.

    A forty-job transcript sweep at concurrency 8 finished twenty-two jobs in
    its first fifteen seconds and then fell to roughly one per fifteen seconds
    for the remaining three and a half minutes. The only failures in it were
    seven videos with captions turned off — a fact about those videos, which
    the worker reported to the controller as throttling, doubling the lane's
    minimum interval each time until it reached the tail rate above.
    """
    source = FailingSource()
    database, _, payloads = build(tmp_path, database, source)
    for index in range(3):
        enqueue(database, "video.failing", f"video{index:06d}")
    controller = RateController(window_ceiling=6)
    for _ in range(8):
        controller.record("direct", Lane.YOUTUBE, Verdict.OK)
    widened = controller.window("direct", Lane.YOUTUBE)
    worker = Worker(
        database=database,
        registry=_registry(source),
        payloads=payloads,
        name="worker-1",
        controller=controller,
    )

    worker.drain()

    assert controller.window("direct", Lane.YOUTUBE) == widened
    assert controller.is_available("direct", Lane.YOUTUBE)


def test_the_jobs_a_listing_fans_out_carry_their_own_kinds_bound(
    tmp_path: Path, database: Database
) -> None:
    """The fan-out is where forgetting this is most expensive.

    One channel becomes a job per video it holds, so a bound that is not
    applied here is not applied a hundred times — and `--then video.comments`
    is the shape that turns one queued channel into a hundred harvests.
    """
    listing = ListingSource()
    expensive = EchoSource()
    expensive.kind = "video.expensive"  # type: ignore[misc]
    expensive.cost = SourceCost.EXPENSIVE  # type: ignore[misc]
    worker = Worker(
        database=database,
        registry=_registry(listing, expensive),
        payloads=PayloadStore(tmp_path / "payloads"),
        name="worker-1",
        concurrency=1,
    )
    with database.session() as session:
        session.add(Job(kind="channel.fake", target="@someone", follow_up_kind="video.expensive"))

    worker.run_once()

    with database.session() as session:
        bounds = {
            job.max_attempts
            for job in session.query(Job).filter(Job.kind == "video.expensive").all()
        }
    assert bounds and bounds != {3}, (
        f"the follow-up jobs took the column default instead of their kind's bound: {bounds}"
    )


def test_a_paused_worker_claims_nothing(tmp_path: Path, database: Database) -> None:
    """The one control an operator actually needs, and the only shared channel is the row.

    The worker is a separate process from the API — that split is deliberate,
    so a yt-dlp crash cannot take the API down — which means a pause button
    cannot reach into its memory. It reads the flag at the top of a drain
    instead, and `tubedepth work` drains and exits, so the systemd restart loop
    is what makes the pause take effect within seconds.
    """
    from tubedepth.models import WorkerControl

    source = EchoSource()
    database, worker, _ = build(tmp_path, database, source)
    enqueue(database, "video.echo", "dQw4w9WgXcQ")
    with database.session() as session:
        session.add(WorkerControl(identifier="worker", paused=True))

    completed = worker.drain()

    assert completed == 0
    assert source.calls == [], "a paused worker collected anyway"
    with database.session() as session:
        assert session.query(Job).filter(Job.state == JobState.QUEUED).count() == 1


def test_resuming_lets_the_queue_move_again(tmp_path: Path, database: Database) -> None:
    """Paused is a state, not a discarded queue: nothing is failed or cancelled
    on the way in, so resuming is the whole of the undo."""
    from tubedepth.models import WorkerControl

    source = EchoSource()
    database, worker, _ = build(tmp_path, database, source)
    enqueue(database, "video.echo", "dQw4w9WgXcQ")
    with database.session() as session:
        session.add(WorkerControl(identifier="worker", paused=True))
    worker.drain()

    with database.session() as session:
        session.get(WorkerControl, "worker").paused = False  # type: ignore[union-attr]
    completed = worker.drain()

    assert completed == 1
    assert source.calls == ["dQw4w9WgXcQ"]


def test_pausing_partway_through_stops_the_drain(tmp_path: Path, database: Database) -> None:
    """A pause has to bite during a sweep, which is the only time it is wanted.

    The check used to run once, at the top of the drain. That was defensible
    when a drain was a handful of jobs and the unit restarted the process every
    ten seconds — but a batch is up to 500 targets, `--from-file` points at a
    watch list, and one listing fans out to the whole listing cap. A drain is
    now long enough that "it takes effect on the next restart" means "not until
    the sweep it is trying to stop has finished".
    """
    from tubedepth.models import WorkerControl

    class PausesItself(EchoSource):
        kind = "video.pauses"

        def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> EchoPayload:
            with database.session() as session:
                session.add(WorkerControl(identifier="worker", paused=True))
            return super().collect(target, egress, runtime)

    source = PausesItself()
    worker = Worker(
        database=database,
        registry=_registry(source),
        payloads=PayloadStore(tmp_path / "payloads"),
        name="worker-1",
        concurrency=1,
    )
    for index in range(4):
        enqueue(database, "video.pauses", f"video{index:06d}")

    completed = worker.drain()

    assert completed == 1, f"the pause was ignored for the rest of the drain: {completed} ran"
    with database.session() as session:
        assert session.query(Job).filter(Job.state == JobState.QUEUED).count() == 3


def test_a_paused_worker_still_announces_what_it_already_finished(
    tmp_path: Path, database: Database
) -> None:
    """Pause means claim nothing. It does not mean stop talking.

    A job that succeeded moments before the pause is owed a callback, and a
    receiver waiting on one cannot tell "the operator paused collection" from
    "my job has not finished". Reaping is the same argument: rows a killed
    worker left in `running` are not the operator's doing and should not wait
    for a resume.
    """
    import respx

    from tubedepth.models import WorkerControl

    with respx.mock:
        route = respx.post("https://example.invalid/hook").respond(200)
        with database.session() as session:
            session.add(
                Job(
                    kind="video.echo",
                    target="video000001",
                    state=JobState.SUCCEEDED,
                    finished_at=utcnow(),
                    webhook_url="https://example.invalid/hook",
                )
            )
            session.add(WorkerControl(identifier="worker", paused=True))

        Worker(
            database=database,
            registry=_registry(EchoSource()),
            payloads=PayloadStore(tmp_path / "payloads"),
            name="worker-1",
            webhook_secret="shh",
        ).drain()

        assert route.called, "a pause silenced a callback the job had already earned"


# -- staying up ---------------------------------------------------------
#
# `tubedepth work` drained the queue and exited, and the unit's
# `Restart=always` + `RestartSec=10s` was the polling loop. It worked, and it
# cost a process launch every ten seconds — measured on this host at ~520 ms of
# CPU and a 68 MB peak, 589 restarts in the first stretch of a day, for a queue
# that was usually empty. On SQLite that is waste. On the shared PostgreSQL
# this is moving to it is connection churn against a budget other services draw
# on, which `docs/shared-postgres.md` counts as a fleet-wide number.


def until(condition: Callable[[], bool], *, seconds: float = 5.0) -> bool:
    """Wait for something a worker thread is doing, or give up.

    A bound rather than an unbounded wait: a broken loop should fail this suite
    in seconds with an assertion, not hang the run.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


@contextmanager
def serving(worker: Worker, *, poll: float = 0.01) -> Iterator[list[int]]:
    """Run `serve` on a thread for the body, and stop it on the way out.

    Threaded rather than stepped through a hook, because the behaviour under
    test is that the process stays up between drains — and a hook that let the
    test drive each iteration would be test-only machinery on the worker,
    proving that the hook works.
    """
    stop = threading.Event()
    completed: list[int] = []
    thread = threading.Thread(target=lambda: completed.append(worker.serve(poll=poll, stop=stop)))
    thread.start()
    try:
        yield completed
    finally:
        stop.set()
        thread.join(timeout=10)
        assert not thread.is_alive(), "serve did not return after its stop event was set"


def test_a_job_queued_after_the_queue_emptied_is_still_collected(
    tmp_path: Path, database: Database
) -> None:
    """The whole point: an empty queue is not a reason to exit.

    The job is queued after the worker has already started and found nothing,
    so only a process that stayed up can collect it. This is what makes the
    restart loop unnecessary rather than merely cheaper.
    """
    source = EchoSource()
    database, worker, _ = build(tmp_path, database, source)

    with serving(worker):
        assert until(lambda: worker.drains > 0), "the first drain never ran"
        enqueue(database, "video.echo", "dQw4w9WgXcQ")

        assert until(lambda: source.calls == ["dQw4w9WgXcQ"]), (
            f"the worker exited at the first empty queue: {source.calls}"
        )


def test_serving_reports_what_it_collected_across_every_drain(
    tmp_path: Path, database: Database
) -> None:
    """One number for the whole run, not the last drain's.

    The CLI prints it, and a resident worker reporting `0 job(s)` after an hour
    of collecting would make the one line an operator reads a lie.
    """
    source = EchoSource()
    database, worker, _ = build(tmp_path, database, source)

    with serving(worker) as completed:
        enqueue(database, "video.echo", "dQw4w9WgXcQ")
        assert until(lambda: source.calls == ["dQw4w9WgXcQ"])
        enqueue(database, "video.echo", "9bZkp7q19f0")
        assert until(lambda: source.calls == ["dQw4w9WgXcQ", "9bZkp7q19f0"])

    assert completed == [2]


def test_a_stop_during_the_wait_is_not_made_to_wait_it_out(
    tmp_path: Path, database: Database
) -> None:
    """SIGINT has to be prompt, and a plain sleep would make it take `poll`.

    The unit sends SIGINT and allows 120 seconds, so a sleeping worker would
    still shut down eventually — but every stop would cost the full interval,
    and the operator watching `systemctl stop` cannot tell that from a hang.
    The wait is the stop event's own, so setting it returns at once.
    """
    _, worker, _ = build(tmp_path, database, EchoSource())
    stop = threading.Event()
    threading.Timer(0.05, stop.set).start()

    started = time.monotonic()
    worker.serve(poll=30.0, stop=stop)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"a 30s poll made a stop take {elapsed:.1f}s"


def test_a_drain_that_did_work_goes_straight_round_again(
    tmp_path: Path, database: Database
) -> None:
    """No pause while there is work.

    A listing fans out to a job per video, so the drain that collects the
    listing leaves the queue fuller than it found it. Waiting `poll` seconds
    there would add an interval per level of fan-out for nothing.
    """
    source = EchoSource()
    database, worker, _ = build(tmp_path, database, source)
    for index in range(3):
        enqueue(database, "video.echo", f"video-idx{index}")
    waits: list[float] = []

    def stop_at_the_first_wait(seconds: float) -> bool:
        waits.append(seconds)
        return True

    worker._wait = stop_at_the_first_wait  # type: ignore[method-assign]

    completed = worker.serve(poll=7.0, stop=threading.Event())

    assert completed == 3
    assert waits == [7.0], f"it waited between drains that had work: {waits}"


def test_serving_survives_a_drain_that_raises(tmp_path: Path, database: Database) -> None:
    """A resident worker cannot inherit the restart loop's forgiveness.

    Under `Restart=always` an unhandled exception was a ten-second gap and a
    fresh process. Staying up means one would end collection until somebody
    noticed, so a failing drain is logged and retried on the next tick — the
    outcome the restart gave, without the restart.

    Deliberately not narrowed to a known error type: the property is that no
    exception from a drain ends the loop, and naming a subset would be a list
    to keep in step with everything a source might raise.
    """
    _, worker, _ = build(tmp_path, database, EchoSource())
    stop = threading.Event()
    attempts = 0

    def explode(*, limit: int | None = None) -> int:
        nonlocal attempts
        attempts += 1
        if attempts >= 2:
            stop.set()
        raise RuntimeError("the database went away")

    worker.drain = explode  # type: ignore[method-assign]

    worker.serve(poll=0.0, stop=stop)

    assert attempts >= 2, "one failing drain ended the loop"
