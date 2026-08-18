"""Query objects. Each takes the Session it works in."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Job, JobState, utcnow


class JobRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

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
        now = utcnow()
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
