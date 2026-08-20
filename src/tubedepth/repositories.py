"""Query objects. Each takes the Session it works in."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .errors import ConflictError, NotFoundError
from .models import Artifact, Job, JobState, utcnow

# States from which nothing further happens. Cancelling one is a conflict
# rather than a no-op: a client told "cancelled" about work that already ran
# would believe it had prevented a cost it in fact paid.
TERMINAL_STATES = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED})


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

        Under PostgreSQL's READ COMMITTED, two workers can both SELECT the
        same candidate — nothing here escalates a lock ahead of the write the
        way SQLite's BEGIN IMMEDIATE once did. Safety comes entirely from the
        UPDATE that follows: `state == QUEUED` is in its WHERE clause, so of
        two workers racing the same candidate only the one that commits first
        actually flips a still-QUEUED row, and the rowcount check is how the
        loser finds out it got nothing rather than believing it claimed a job
        it did not. That guard is the whole mechanism now, not a second layer
        on top of one.
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

    def cancel(self, identifier: str) -> Job:
        """Stop a job that is no longer wanted, as far as is honest.

        A queued job is cancelled outright: nothing is happening to it, so the
        state change *is* the cancellation and the claim query will never see
        it again.

        A running job is only *marked*. Its extraction is inside yt-dlp, inside
        a thread, and nothing here can interrupt that — so moving it to
        CANCELLED would announce that the cost had stopped while requests were
        still going out against the address. What the mark buys is real but
        narrower: the worker will not retry it, and its result is discarded
        rather than stored. The caller is told which of the two happened by the
        state on the row it gets back.

        A finished job is a conflict rather than a silent no-op. "Cancel"
        succeeding against a job that already ran would let a client believe it
        had prevented work that has in fact been done and billed for.
        """
        job = self._session.get(Job, identifier)
        if job is None:
            raise NotFoundError(f"no such job: {identifier}")
        if job.state in TERMINAL_STATES:
            raise ConflictError(f"job has already finished: {identifier}")

        now = self._clock()
        job.cancel_requested_at = now
        if job.state is JobState.QUEUED:
            job.state = JobState.CANCELLED
            job.finished_at = now
            job.claimed_by = None
            job.lease_expires_at = None
        return job

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


class ArtifactRepository:
    def __init__(self, session: Session, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._session = session
        self._clock = clock

    def record(
        self,
        *,
        kind: str,
        target: str,
        fingerprint: str,
        digest: str,
        byte_count: int,
        freshness: timedelta,
        schema_version: str,
    ) -> Artifact:
        now = self._clock()
        artifact = Artifact(
            kind=kind,
            target=target,
            fingerprint=fingerprint,
            schema_version=schema_version,
            digest=digest,
            byte_count=byte_count,
            fetched_at=now,
            fresh_until=now + freshness,
        )
        self._session.add(artifact)
        return artifact

    def fresh(self, fingerprint: str) -> Artifact | None:
        """The newest still-good answer to this question, if there is one."""
        return self._session.scalars(
            select(Artifact)
            .where(Artifact.fingerprint == fingerprint, Artifact.fresh_until > self._clock())
            .order_by(Artifact.fetched_at.desc())
            .limit(1)
        ).first()

    def count_for(self, fingerprint: str) -> int:
        return len(
            self._session.scalars(
                select(Artifact.identifier).where(Artifact.fingerprint == fingerprint)
            ).all()
        )
