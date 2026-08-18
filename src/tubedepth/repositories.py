"""Query objects. Each takes the Session it works in."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Job, JobState, utcnow


class JobRepository:
    def __init__(self, session: Session, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._session = session
        # Injected so the time-dependent paths — leases, backoff — are testable
        # without sleeping, which is the only way they get tested at all.
        self._clock = clock

    def claim(
        self, *, worker: str, lease: timedelta, kinds: Sequence[str] | None = None
    ) -> Job | None:
        """Take exactly one queued job, or return None.

        The write lock is held from the first statement of the transaction —
        see Database, which makes every transaction IMMEDIATE — so the SELECT
        below cannot be overtaken by another worker's UPDATE before this one
        runs.

        The `state == QUEUED` guard on the UPDATE plus the rowcount check is
        belt and braces on top of that.
        """
        now = self._clock()
        conditions = [Job.state == JobState.QUEUED, Job.scheduled_at <= now]
        if kinds is not None:
            # Filtered in the claim itself rather than claimed and put back: a
            # job returned to the queue has burned an attempt and lost its place.
            conditions.append(Job.kind.in_(kinds))

        candidate = self._session.scalars(
            select(Job.identifier)
            .where(*conditions)
            .order_by(Job.scheduled_at, Job.created_at)
            .limit(1)
        ).one_or_none()
        if candidate is None:
            return None

        # Through the session's connection rather than the session: both run
        # in the same transaction, but Connection.execute is typed as returning
        # a CursorResult, which is what actually carries rowcount. Session.execute
        # is typed as Result and reaching for rowcount there needs a cast that
        # would be asserting something the type checker cannot see.
        taken = self._session.connection().execute(
            update(Job)
            .where(Job.identifier == candidate, Job.state == JobState.QUEUED)
            .values(
                state=JobState.RUNNING,
                claimed_by=worker,
                lease_expires_at=now + lease,
                attempt_count=Job.attempt_count + 1,
            )
        )
        if taken.rowcount != 1:
            return None
        return self._session.get(Job, candidate)

    def renew_lease(self, identifier: str, *, lease: timedelta) -> None:
        """Push a running job's lease out.

        A comment harvest can outlive its lease, and being reaped mid-run is
        worse than slow: the job runs twice, against the same address, for
        nothing.
        """
        job = self._session.get(Job, identifier)
        if job is not None and job.state is JobState.RUNNING:
            job.lease_expires_at = self._clock() + lease

    def reap_expired_leases(self) -> int:
        """Return jobs whose worker stopped reporting, and count the attempt.

        A worker killed with SIGKILL cannot release anything, so its job has to
        time out instead. Counting the attempt is what stops a job that kills
        every worker it touches from being retried forever.
        """
        now = self._clock()
        expired = self._session.scalars(
            select(Job).where(
                Job.state == JobState.RUNNING,
                Job.lease_expires_at.is_not(None),
                Job.lease_expires_at < now,
            )
        ).all()

        for job in expired:
            job.claimed_by = None
            job.lease_expires_at = None
            if job.attempt_count >= job.max_attempts:
                job.state = JobState.FAILED
                job.finished_at = now
                job.error_code = "lease_expired"
                job.error_message = (
                    f"lease expired {job.attempt_count} time(s) without the job finishing"
                )
            else:
                job.state = JobState.QUEUED
        return len(expired)
