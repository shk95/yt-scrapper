"""Getting back the jobs a dead worker was holding.

A worker killed with SIGKILL cannot release anything, so its job has to time
out instead. Without that, one crash strands a row in `running` forever and
the only symptom is a queue that quietly stops shrinking.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tubedepth.database import Database
from tubedepth.models import Job, JobState
from tubedepth.repositories import JobRepository

LEASE = timedelta(minutes=15)
START = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class FakeClock:
    """A clock the test drives, so nothing here sleeps."""

    def __init__(self, now: datetime = START) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def prepared(tmp_path: Path, clock: FakeClock, *kinds: str) -> Database:
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    with database.session() as session:
        for kind in kinds:
            session.add(
                Job(kind=kind, target="dQw4w9WgXcQ", created_at=clock(), scheduled_at=clock())
            )
    return database


def test_a_job_whose_lease_expired_returns_to_the_queue(tmp_path: Path) -> None:
    clock = FakeClock()
    database = prepared(tmp_path, clock, "video.metadata")
    with database.session() as session:
        JobRepository(session, clock=clock).claim(worker="doomed", lease=LEASE)

    clock.advance(LEASE + timedelta(seconds=1))
    with database.session() as session:
        reaped = JobRepository(session, clock=clock).reap_expired_leases()

    assert reaped == 1
    with database.session() as session:
        job = session.query(Job).one()
        assert job.state is JobState.QUEUED
        assert job.claimed_by is None
        assert job.lease_expires_at is None


def test_a_reaped_job_keeps_the_attempt_that_was_spent_on_it(tmp_path: Path) -> None:
    # Otherwise a job that crashes a worker every time is retried forever, and
    # the attempt limit that exists to stop that never counts anything.
    clock = FakeClock()
    database = prepared(tmp_path, clock, "video.metadata")
    with database.session() as session:
        JobRepository(session, clock=clock).claim(worker="doomed", lease=LEASE)
    clock.advance(LEASE + timedelta(seconds=1))

    with database.session() as session:
        JobRepository(session, clock=clock).reap_expired_leases()

    with database.session() as session:
        assert session.query(Job).one().attempt_count == 1


def test_a_job_that_has_used_every_attempt_is_failed_rather_than_requeued(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    database = prepared(tmp_path, clock, "video.metadata")
    with database.session() as session:
        session.query(Job).one().max_attempts = 1
    with database.session() as session:
        JobRepository(session, clock=clock).claim(worker="doomed", lease=LEASE)
    clock.advance(LEASE + timedelta(seconds=1))

    with database.session() as session:
        JobRepository(session, clock=clock).reap_expired_leases()

    with database.session() as session:
        job = session.query(Job).one()
        assert job.state is JobState.FAILED
        assert job.error_code == "lease_expired"
        assert "lease" in (job.error_message or "")


def test_a_job_whose_lease_is_still_valid_is_left_alone(tmp_path: Path) -> None:
    clock = FakeClock()
    database = prepared(tmp_path, clock, "video.metadata")
    with database.session() as session:
        JobRepository(session, clock=clock).claim(worker="busy", lease=LEASE)

    clock.advance(LEASE - timedelta(minutes=1))
    with database.session() as session:
        assert JobRepository(session, clock=clock).reap_expired_leases() == 0

    with database.session() as session:
        assert session.query(Job).one().state is JobState.RUNNING


def test_renewing_a_lease_keeps_a_long_job_out_of_the_reaper(tmp_path: Path) -> None:
    """A harvest can outlive its lease, and being reaped mid-run is worse than
    slow: the job runs twice, against the same address, for nothing.
    """
    clock = FakeClock()
    database = prepared(tmp_path, clock, "video.comments")
    with database.session() as session:
        claimed = JobRepository(session, clock=clock).claim(worker="slow", lease=LEASE)
        assert claimed is not None
        identifier = claimed.identifier

    clock.advance(LEASE - timedelta(minutes=1))
    with database.session() as session:
        JobRepository(session, clock=clock).renew_lease(identifier, lease=LEASE)

    clock.advance(timedelta(minutes=2))
    with database.session() as session:
        assert JobRepository(session, clock=clock).reap_expired_leases() == 0
