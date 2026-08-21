"""The database tables.

SQLAlchemy 2.0 declarative, mapped with Mapped[...] / mapped_column.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Enum,
    Float,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_identifier() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A datetime that is aware UTC on both sides of the database.

    SQLite has no datetime type and no timezone, so a value written as aware
    UTC reads back naive. The application no longer runs on SQLite (#15), but
    `tubedepth transfer --from` still reads a real SQLite index through these
    same mapped models — that is the one place this class's SQLite behaviour
    still runs, and it is why the naive-value refusal below cannot be deleted
    along with the rest of SQLite support. Everything here treats stored
    instants as aware — the public contract says so — and a naive one does
    not raise on comparison, it silently compares wrong. Putting the offset
    back on load is the only place that can be fixed once rather than at
    every call site.

    `impl` carries `timezone=True` so autogenerate's type comparator agrees
    with what `migrations/env.py`'s `render_item` renders and what the
    database actually stores: `docs/shared-postgres.md` rule 9 requires
    `timestamptz` for every instant, and on PostgreSQL a plain `DateTime()`
    impl compares as `timestamp without time zone` against the reflected
    `timestamptz` column, which is exactly the mismatch that makes autogenerate
    propose a spurious `modify_type`. SQLite ignores the flag — it has no
    timezone-aware storage either way — so this is a no-op on the SQLite
    source `transfer` reads from.
    """

    impl = DateTime(timezone=True)
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
    # Whether this submission asked to collect even if a fresh artifact is
    # held. On the row because the collection happens in another process
    # minutes later: a flag consumed at the HTTP boundary is one the worker
    # never sees, and `refresh` spent a release changing only which of 200 or
    # 202 the API answered while the job it created was still served from the
    # cache. Deliberately not indexed — the claim filters on state and
    # scheduled_at and never asks this.
    refresh: Mapped[bool] = mapped_column(default=False)
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
    # Where to announce this job's end, if the submitter asked for one. The
    # attempt count lives beside it so a receiver that has been down all day
    # stops being hammered without the sender needing its own table.
    webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    webhook_attempts: Mapped[int] = mapped_column(default=0)
    webhook_delivered_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

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
    # Which version of this kind's normalizer wrote the payload. The
    # fingerprint already contains it and is a SHA-256, so it cannot be read
    # back out — hold an old payload and there is no way to tell what shape it
    # is in, which is what makes a bump sever history rather than age it.
    #
    # Nullable permanently, and not only because SQLite refuses `ADD COLUMN
    # ... NOT NULL` without a default. Any default would be a claim: it would
    # make every row that predates this column assert a version nobody
    # recorded, and `channel.about` is already at "2" — so "1" would be wrong
    # for exactly the rows whose contents are known to be wrong. Null means
    # "written before this was recorded", which is what the backfill selects on.
    schema_version: Mapped[str | None] = mapped_column(String(16), nullable=True)

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


# One row, addressed by a fixed key. See WorkerControl.
WORKER_CONTROL_ID = "worker"


class WorkerControl(Base):
    """What an operator has told the worker to do. One row.

    The worker is a separate process from the API on purpose — a yt-dlp crash
    must not take the API down — so nothing in the API can reach into its
    memory to stop it. A row is the only channel the two share, and it is the
    same channel `source_health` and `lane_health` already use, running the
    other way.

    A single row rather than a settings table: there is one worker, and a
    schema that implies several would be a promise nothing here keeps.
    """

    __tablename__ = "worker_control"

    # Fixed. The primary key exists so the row is addressable, not so there
    # can be more than one of them.
    identifier: Mapped[str] = mapped_column(String(32), primary_key=True, default="worker")
    # Paused means "claim nothing", not "discard". Queued jobs stay queued and
    # nothing is failed on the way in, so resuming is the whole of the undo.
    paused: Mapped[bool] = mapped_column(default=False)
    # Why, in the operator's words. A pause nobody can explain an hour later is
    # a pause nobody dares lift.
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class LaneHealth(Base):
    """One row per (egress, lane): what the rate controller currently believes.

    Same argument as SourceHealth, one level along — the controller's state is
    a dict in the worker's memory and dies with the process, while the question
    "is this route being refused right now" is asked by the API and by whoever
    is looking at the dashboard when collection has stopped.

    Without this the only symptom of a quarantined lane is that nothing is
    happening, which is indistinguishable from an empty queue.
    """

    __tablename__ = "lane_health"

    egress: Mapped[str] = mapped_column(String(64), primary_key=True)
    lane: Mapped[str] = mapped_column(String(32), primary_key=True)
    # How many requests the controller will allow at once. It is a measured
    # ceiling rather than a setting: it halves on a refusal and grows back.
    window: Mapped[float] = mapped_column(Float, default=1.0)
    in_flight: Mapped[int] = mapped_column(default=0)
    quarantine_streak: Mapped[int] = mapped_column(default=0)
    # Wall clock, converted at write time. The controller measures in
    # `time.monotonic` because this host's wall clock jumps after the Windows
    # host sleeps — but a reader needs a time it can compare to its own, and a
    # monotonic reading from another process means nothing here.
    quarantined_until: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


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


class VideoSnapshot(Base):
    """One video.metadata observation, flattened for SQL.

    Deliberately no foreign key to `artifacts`: retention deletes artifact
    rows after their window, and these rows are the long-lived series that
    is meant to survive that. `artifact_id` is provenance and the
    idempotency key, not a reference anything enforces.
    """

    __tablename__ = "video_snapshots"
    __table_args__ = (Index("ix_video_snapshot_series", "video_id", "fetched_at"),)

    artifact_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    video_id: Mapped[str] = mapped_column(String(500), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    view_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    like_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    comment_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # The coarse fallback for the exact instant above; YouTube stopped
    # returning `published_at` for some videos (see schemas.VideoMetadata).
    published_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class ListingEntry(Base):
    """One position in one listing observation — a ranking time series."""

    __tablename__ = "listing_entries"
    __table_args__ = (
        Index("ix_listing_entry_series", "target", "fetched_at"),
        Index("ix_listing_entry_video", "video_id"),
    )

    artifact_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 0-based position within the listing at observation time.
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(500), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    video_id: Mapped[str] = mapped_column(String(500), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    view_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class ChannelSnapshot(Base):
    """One channel.about observation, flattened for SQL."""

    __tablename__ = "channel_snapshots"
    __table_args__ = (Index("ix_channel_snapshot_series", "channel_id", "fetched_at"),)

    artifact_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(500), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    handle: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Rounded by YouTube; the name says so. Nothing more precise exists.
    subscriber_count_approximate: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    view_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    video_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)


class CommentRecord(Base):
    """One comment, deduplicated across harvests.

    Harvests overlap: the same comment appears in every 24h harvest of its
    video. This table keeps one row per (video_id, comment_id); mutable
    fields follow the newest observation, `first_seen_at`/`last_seen_at`
    record the observed lifespan.
    """

    __tablename__ = "comments"
    __table_args__ = (Index("ix_comment_published", "video_id", "published_at"),)

    # The harvest payload does not carry the video id; the artifact's
    # `target` is it.
    video_id: Mapped[str] = mapped_column(String(500), primary_key=True)
    comment_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    # None means top-level, deliberately not a sentinel string
    # (see schemas.Comment).
    parent_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    like_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_hearted_by_uploader: Mapped[bool] = mapped_column(nullable=False, default=False)
    is_pinned: Mapped[bool] = mapped_column(nullable=False, default=False)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class TranscriptRecord(Base):
    """The newest transcript per (video, language)."""

    __tablename__ = "transcripts"

    video_id: Mapped[str] = mapped_column(String(500), primary_key=True)
    language: Mapped[str] = mapped_column(String(64), primary_key=True)
    is_automatic: Mapped[bool] = mapped_column(nullable=False, default=False)
    full_text: Mapped[str] = mapped_column(Text, nullable=False)
    segment_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


# One row, addressed by a fixed key. Same pattern as WorkerControl.
FLATTEN_PROGRESS_ID = "flatten"


class FlattenProgress(Base):
    """Where the flatten pass has read to. One row.

    The cursor is the pair `(cursor_fetched_at, cursor_identifier)` because
    `fetched_at` alone is not a total order — two observations can share an
    instant, and a cursor that cannot break the tie either re-reads or
    skips one of them.
    """

    __tablename__ = "flatten_progress"

    identifier: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=FLATTEN_PROGRESS_ID
    )
    cursor_fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    cursor_identifier: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
