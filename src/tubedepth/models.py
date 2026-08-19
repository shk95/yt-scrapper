"""The database tables.

SQLAlchemy 2.0 declarative, mapped with Mapped[...] / mapped_column.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, Index, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_identifier() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A datetime that is aware UTC on both sides of the database.

    SQLite has no datetime type and no timezone, so a value written as aware
    UTC reads back naive. Everything here treats stored instants as aware —
    the public contract says so — and a naive one does not raise on comparison,
    it silently compares wrong. Putting the offset back on load is the only
    place that can be fixed once rather than at every call site.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"refusing to store a naive datetime: {value!r}")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class JobState(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Asked for and no longer wanted. Reached directly from QUEUED, and from
    # RUNNING only once the worker notices — see JobRepository.cancel for why
    # a running job is not moved here by the request itself.
    CANCELLED = "cancelled"


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        # The claim's index, and the column order matches its WHERE and ORDER
        # BY exactly. Without it every claim is a full scan plus a temporary
        # B-tree for the ordering — on the hot path of every job the system
        # runs. The plan called for this and it was never added; five hundred
        # rows hid it.
        Index("ix_job_claimable", "state", "scheduled_at", "created_at"),
        # The reaper's, which asks a different question of the same column.
        Index("ix_job_lease", "state", "lease_expires_at"),
        # Browsing, which filters by kind and orders by recency. Deliberately
        # separate from the claim index: leading with `state` would make it
        # useless for a browser that does not filter on state.
        Index("ix_job_recent", "kind", "created_at"),
    )

    identifier: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_identifier)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # What to collect for each video a listing job finds. Null means enumerate
    # and stop, which is a legitimate thing to want: checking what a channel
    # holds should not cost a hundred extractions.
    follow_up_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Which key submitted this. How a runaway client gets identified rather
    # than guessed at.
    api_key_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
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
    scheduled_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # Set the moment cancellation is asked for, and never cleared. For a queued
    # job it is the same instant as `finished_at`; for a running one it is the
    # only record that the request happened at all, since the job goes on to
    # finish or fail on its own.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # The result, by reference. The payload itself is a file: a comment harvest
    # runs to tens of megabytes and does not belong in the table the claim
    # query depends on staying fast.
    payload_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Why it failed, on the row. A job that only says "failed" sends whoever is
    # on call to the logs for what was already known at the moment it happened.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class Artifact(Base):
    """One observation: what was collected, when, and where the bytes are.

    Rows are the index; the payload itself is a file. A comment harvest runs
    to tens of megabytes and does not belong in the table the claim query
    depends on staying fast.

    Deliberately no unique constraint on `fingerprint`. Observations accumulate,
    so how a video's view and like counts moved is a by-product of caching
    rather than a separate feature. The cost is disk, which retention bounds.
    """

    __tablename__ = "artifacts"
    __table_args__ = (
        # The cache path: is there a fresh answer to this exact question.
        Index("ix_artifact_lookup", "fingerprint", "fresh_until"),
        # Browsing by kind, newest first.
        Index("ix_artifact_recent", "kind", "fetched_at"),
        # One target's history, which is what this table keeps by appending
        # rather than overwriting — how a video's counts moved over a month.
        Index("ix_artifact_target", "target", "fetched_at"),
    )

    identifier: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_identifier)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(500), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)

    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    # Materialized rather than computed, so "is this still good" is an indexed
    # comparison instead of a per-row calculation.
    fresh_until: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class ApiKey(Base):
    """A credential, stored as a hash.

    The plaintext is shown once at creation and never again: a database that
    leaks should not leak working credentials with it, and that property is
    only real if nothing keeps a copy.

    In the database rather than a config file because revocation has to take
    effect on the next request rather than the next restart, and because a job
    carrying `api_key_id` is how a runaway client gets identified.
    """

    __tablename__ = "api_keys"

    identifier: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_identifier)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    # Indexed, so verification is one lookup plus one constant-time compare
    # rather than hashing the presented key against every row.
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    # sha256 rather than a password hash, deliberately: these are 192 bits of
    # CSPRNG output, not a user-chosen password. There is no dictionary to run
    # and no work factor worth paying on every request.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    requests_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class SourceHealth(Base):
    """One row per kind: what that source has been doing lately.

    In the database rather than in the worker's memory because the process
    asking is not the process that knows. The rate controller's state dies with
    the worker; this has to outlive it and be readable by the API.
    """

    __tablename__ = "source_health"

    kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    blocked: Mapped[bool] = mapped_column(default=False)
    last_success_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
