"""The database tables.

SQLAlchemy 2.0 declarative, mapped with Mapped[...] / mapped_column.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, Integer, String
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


class Job(Base):
    __tablename__ = "jobs"

    identifier: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_identifier)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[JobState] = mapped_column(
        # native_enum=False keeps these as TEXT, so adding a member never needs
        # a migration — and the job kinds this project grows are exactly the
        # thing that will keep being added.
        Enum(JobState, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=JobState.QUEUED,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
