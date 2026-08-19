"""What each source has been doing lately, kept where the API can read it.

The gap this closes: the rate controller knows when a route is in trouble and
that knowledge lives in the worker's memory, so nothing outside the worker
process can see it. An operator asking "why is nothing arriving" has had only
the job table, which says what failed but not whether anything is wrong now.

Kept per source rather than per lane because the question being asked is
different. The lane answers "may I make another request"; this answers "is
this kind of collection still working", which is what goes quiet when YouTube
renames a renderer and `video.related` starts returning nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tubedepth.database import Database
from tubedepth.errors import ExtractionError, NotFoundError, RateLimitedError
from tubedepth.health import SourceHealthService


def service(tmp_path: Path, now: datetime | None = None) -> tuple[Database, SourceHealthService]:
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    moment = now or datetime(2026, 8, 19, 12, tzinfo=UTC)
    return database, SourceHealthService(database=database, clock=lambda: moment)


def test_a_source_that_has_never_run_is_reported_as_unknown(tmp_path: Path) -> None:
    """Not healthy. A dashboard that shows green for something never tried is
    worse than one that admits it does not know."""
    _, health = service(tmp_path)

    assert health.snapshot()["video.related"].status == "unknown"


def test_a_succeeding_source_is_healthy(tmp_path: Path) -> None:
    _, health = service(tmp_path)

    health.record("video.related", succeeded=True)

    entry = health.snapshot()["video.related"]
    assert entry.status == "healthy"
    assert entry.consecutive_failures == 0
    assert entry.last_success_at is not None


def test_repeated_extraction_failures_mark_a_source_broken(tmp_path: Path) -> None:
    """The InnerTube case this exists for.

    A renamed renderer makes every call fail identically, which is a fact about
    our parser rather than about the network — and it is invisible today except
    as rows in the job table nobody is watching.
    """
    _, health = service(tmp_path)

    for _ in range(3):
        health.record("video.related", succeeded=False, error=ExtractionError("no renderer"))

    entry = health.snapshot()["video.related"]
    assert entry.status == "broken"
    assert entry.consecutive_failures == 3
    assert entry.last_error_code == "ExtractionError"


def test_one_failure_is_not_yet_a_broken_source(tmp_path: Path) -> None:
    _, health = service(tmp_path)

    health.record("video.related", succeeded=False, error=ExtractionError("no renderer"))

    assert health.snapshot()["video.related"].status == "degraded"


def test_a_success_clears_the_streak(tmp_path: Path) -> None:
    _, health = service(tmp_path)
    for _ in range(3):
        health.record("video.related", succeeded=False, error=ExtractionError("no renderer"))

    health.record("video.related", succeeded=True)

    entry = health.snapshot()["video.related"]
    assert entry.status == "healthy"
    assert entry.consecutive_failures == 0


def test_a_video_lacking_the_data_is_not_a_sick_source(tmp_path: Path) -> None:
    """The same distinction the rate controller needed, in a second place.

    A video with captions turned off makes `video.transcript` fail, repeatedly
    and legitimately, and a sweep of such videos would otherwise paint the
    source red while it is working perfectly. Only failures that say something
    about *us or the upstream* count.
    """
    _, health = service(tmp_path)

    for _ in range(5):
        health.record("video.transcript", succeeded=False, error=NotFoundError("no captions"))

    entry = health.snapshot()["video.transcript"]
    assert entry.status == "unknown", "a fact about the video is not a verdict on the source"
    assert entry.consecutive_failures == 0


def test_being_blocked_is_recorded_as_blocked_not_broken(tmp_path: Path) -> None:
    """Broken means our parser; blocked means the address. Different fixes."""
    _, health = service(tmp_path)

    for _ in range(3):
        health.record("video.metadata", succeeded=False, error=RateLimitedError("bot check"))

    assert health.snapshot()["video.metadata"].status == "blocked"


def test_health_survives_a_restart(tmp_path: Path) -> None:
    """It is in the database precisely so the API process can read what the
    worker process learned."""
    database, health = service(tmp_path)
    health.record("video.related", succeeded=False, error=ExtractionError("no renderer"))

    reopened = SourceHealthService(database=database)

    assert reopened.snapshot()["video.related"].consecutive_failures == 1


def test_a_source_silent_for_too_long_is_flagged_rather_than_left_green(
    tmp_path: Path,
) -> None:
    """Green-because-nobody-asked is the failure mode of every dashboard.

    A source last seen working a week ago is not evidence that it works now.
    """
    database, health = service(tmp_path)
    health.record("video.related", succeeded=True)

    later = SourceHealthService(
        database=database,
        clock=lambda: datetime(2026, 8, 26, 12, tzinfo=UTC),
        stale_after=timedelta(days=2),
    )

    assert later.snapshot()["video.related"].status == "stale"


def test_the_worker_records_health_as_it_goes(tmp_path: Path) -> None:
    """Otherwise the table is a feature nothing populates.

    `renew_lease` was written, tested and never called; this is the same shape
    of mistake and the check that would have caught it.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_worker import EchoSource, FailingSource, _registry, enqueue

    from tubedepth.payload_store import PayloadStore
    from tubedepth.worker import Worker

    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    enqueue(database, "video.echo", "video000001")
    enqueue(database, "video.failing", "video000002")
    worker = Worker(
        database=database,
        registry=_registry(EchoSource(), FailingSource()),
        payloads=PayloadStore(tmp_path / "payloads"),
        name="worker-1",
    )

    worker.drain()

    health = SourceHealthService(database=database)
    with database.session(readonly=True) as session:
        from tubedepth.models import SourceHealth

        recorded = {row.kind: row for row in session.query(SourceHealth).all()}

    assert recorded["video.echo"].last_success_at is not None
    # FailingSource raises NotFoundError — a fact about the video, so it must
    # not count against the source even though the job failed.
    assert "video.failing" not in recorded or recorded["video.failing"].consecutive_failures == 0
    assert health is not None


def test_the_worker_records_lane_health_as_it_goes(tmp_path: Path) -> None:
    """The check `renew_lease` did not have, applied to the other health table.

    A recorder nothing calls is a table that stays empty while the dashboard
    shows nothing wrong — and "nothing is happening" is exactly what a
    quarantined lane and an empty queue both look like.
    """
    from test_worker import EchoSource, _registry, enqueue

    from tubedepth.models import LaneHealth
    from tubedepth.payload_store import PayloadStore
    from tubedepth.worker import Worker

    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    worker = Worker(
        database=database,
        registry=_registry(EchoSource()),
        payloads=PayloadStore(tmp_path / "payloads"),
        name="worker-1",
        concurrency=1,
    )
    enqueue(database, "video.echo", "dQw4w9WgXcQ")

    worker.drain()

    with database.session() as session:
        recorded = session.query(LaneHealth).all()
        assert [(row.egress, row.lane) for row in recorded] == [("direct", "youtube")]
        assert recorded[0].window > 0
        assert recorded[0].quarantined_until is None


def test_a_quarantined_lane_is_recorded_with_a_deadline_a_reader_can_use(
    tmp_path: Path,
) -> None:
    """The controller measures in `time.monotonic` because this host's wall
    clock jumps after the Windows host sleeps. A monotonic reading from another
    process means nothing, so the deadline is converted where both readings are
    available and only the result is stored."""
    from datetime import UTC, datetime

    from tubedepth.egress.control import Lane, RateController, Verdict
    from tubedepth.health import LaneHealthService
    from tubedepth.models import LaneHealth

    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    controller = RateController()
    controller.record("direct", Lane.YOUTUBE, Verdict.BLOCKED)
    state, monotonic = controller.observed("direct", Lane.YOUTUBE)

    LaneHealthService(database=database).observe(
        "direct", "youtube", state=state, monotonic=monotonic
    )

    with database.session() as session:
        row = session.query(LaneHealth).one()
    assert row.quarantined_until is not None
    assert row.quarantined_until > datetime.now(UTC), "the quarantine was recorded as already over"
