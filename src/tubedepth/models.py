"""The database tables.

SQLAlchemy 2.0 declarative, mapped with Mapped[...] / mapped_column.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_identifier() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class JobState(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Job(Base):
    __tablename__ = "jobs"

    identifier: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_identifier)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # What to collect for each video a listing job finds. Null means enumerate
    # and stop, which is a legitimate thing to want: checking what a channel
    # holds should not cost a hundred extractions.
    follow_up_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[JobState] = mapped_column(
        # native_enum=False keeps these as TEXT, so adding a member never needs
        # a migration — and the job kinds this project grows are exactly the
        # thing that will keep being added.
        Enum(JobState, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=JobState.QUEUED,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Bounded so a job that kills its worker every time cannot be retried
    # forever. Expensive kinds get fewer, set when the job is queued.
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # The result, by reference. The payload itself is a file: a comment harvest
    # runs to tens of megabytes and does not belong in the table the claim
    # query depends on staying fast.
    payload_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Why it failed, on the row. A job that only says "failed" sends whoever is
    # on call to the logs for what was already known at the moment it happened.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
