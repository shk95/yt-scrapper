# Flatten ETL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `tubedepth flatten`, an incremental idempotent ETL that unpacks payload blobs into six queryable PostgreSQL tables, so PostgREST (and data-portal) can serve the contents.

**Architecture:** New models + one Alembic migration create the tables (grants arrive via existing default privileges — nothing outside this repo changes). A pure-function transform module (`flatten.py`) turns payload dicts into row dicts; a `FlattenService` walks `artifacts` in `(fetched_at, identifier)` order behind a persisted cursor, batching upserts and cursor advance in one transaction per batch. A typer CLI command wraps it, one-shot for the systemd timer, `--every` for compose.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 (`Mapped`/`mapped_column`, `postgresql.insert ... on_conflict`), Alembic, typer, pytest against throwaway PostgreSQL (`just test` brings one up).

**Spec:** `docs/superpowers/specs/2026-08-21-flatten-etl-design.md`

## Global Constraints

- Code, comments, docstrings, commit messages: **English**. Contributor docs (status.md): Korean. Conventional Commits; the commit-msg hook refuses anything else.
- Every commit stands alone with `just check` green (`tool/checks/format`, `lint`, `test`; test needs Docker for throwaway PostgreSQL).
- No FK from flattened tables to `artifacts` — retention deletes artifact rows; flattened rows must survive.
- All instants are `UtcDateTime` (timestamptz). Never store naive datetimes.
- Comment/transcript upserts only overwrite when the incoming observation is **newer** (`EXCLUDED.last_seen_at > existing.last_seen_at` / `EXCLUDED.fetched_at > existing.fetched_at`).
- Cursor safety lag: only artifacts with `fetched_at < now() - 5 minutes` are processed.
- Batch defaults: 200 artifacts per transaction (session defaults: statement_timeout 15s, transaction_timeout 60s).
- `tests/test_postgres_migrations.py` enforces models ↔ migrations agreement via `compare_metadata`; `tests/test_deployment_units.py` and `tests/test_compose.py` enforce unit/compose hygiene. Do not weaken these tests — satisfy them.
- Do not touch `docs/api.md`/`api.ko.md` (no new routes). README/README.ko.md get the new command only where they already list commands.

---

### Task 1: Models and migration for the six flatten tables

**Files:**
- Modify: `src/tubedepth/models.py` (append after `SourceHealth`; extend imports)
- Create: `migrations/versions/20260821_<rev>_flatten_tables.py`
- Test: existing `tests/test_postgres_migrations.py` (agreement is already enforced; no new test file)

**Interfaces:**
- Consumes: `Base`, `UtcDateTime`, `utcnow` from `models.py`.
- Produces: model classes `VideoSnapshot`, `ListingEntry`, `ChannelSnapshot`, `CommentRecord`, `TranscriptRecord`, `FlattenProgress`, constant `FLATTEN_PROGRESS_ID = "flatten"` — Tasks 3–4 import these exact names from `tubedepth.models`.

- [ ] **Step 1: Extend imports and append models**

In `src/tubedepth/models.py`, change the datetime import to `from datetime import UTC, date, datetime` and the SQLAlchemy import to include `BigInteger` and `Date`:

```python
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
```

Append at the end of the file:

```python
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
```

- [ ] **Step 2: Find the current migration head**

Run: `uv run alembic heads`
Use the printed revision as `down_revision` in the next step. (Do not trust filename dates — read the command's output.)

- [ ] **Step 3: Write the migration**

Create `migrations/versions/20260821_<rev>_flatten_tables.py`. Generate `<rev>` with `python -c "import uuid; print(uuid.uuid4().hex[:12])"` or run `uv run alembic revision --autogenerate -m "flatten tables"` against the throwaway PostgreSQL and adjust. The hand-written form (mirror the initial-schema style; `sa.DateTime()` columns are rendered as timestamptz by `env.py`'s `render_item` — check how `20260820_55a24ac7a270_instants_are_timestamptz.py` renders types and match it):

```python
"""flatten tables

Revision ID: <rev>
Revises: <down_revision from Step 2>
Created: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "<rev>"
down_revision: str | None = "<down_revision>"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "video_snapshots",
        sa.Column("artifact_id", sa.String(length=32), nullable=False),
        sa.Column("video_id", sa.String(length=500), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=True),
        sa.Column("channel_id", sa.String(length=500), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("view_count", sa.BigInteger(), nullable=True),
        sa.Column("like_count", sa.BigInteger(), nullable=True),
        sa.Column("comment_count", sa.BigInteger(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_date", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("artifact_id"),
    )
    op.create_index(
        "ix_video_snapshot_series", "video_snapshots", ["video_id", "fetched_at"], unique=False
    )

    op.create_table(
        "listing_entries",
        sa.Column("artifact_id", sa.String(length=32), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=500), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("video_id", sa.String(length=500), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("view_count", sa.BigInteger(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("channel", sa.Text(), nullable=True),
        sa.Column("channel_id", sa.String(length=500), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("artifact_id", "position"),
    )
    op.create_index(
        "ix_listing_entry_series", "listing_entries", ["target", "fetched_at"], unique=False
    )
    op.create_index("ix_listing_entry_video", "listing_entries", ["video_id"], unique=False)

    op.create_table(
        "channel_snapshots",
        sa.Column("artifact_id", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sa.String(length=500), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("handle", sa.String(length=500), nullable=True),
        sa.Column("subscriber_count_approximate", sa.BigInteger(), nullable=True),
        sa.Column("view_count", sa.BigInteger(), nullable=True),
        sa.Column("video_count", sa.Integer(), nullable=True),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint("artifact_id"),
    )
    op.create_index(
        "ix_channel_snapshot_series",
        "channel_snapshots",
        ["channel_id", "fetched_at"],
        unique=False,
    )

    op.create_table(
        "comments",
        sa.Column("video_id", sa.String(length=500), nullable=False),
        sa.Column("comment_id", sa.String(length=200), nullable=False),
        sa.Column("parent_id", sa.String(length=200), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("author_id", sa.String(length=500), nullable=True),
        sa.Column("like_count", sa.BigInteger(), nullable=True),
        sa.Column("is_hearted_by_uploader", sa.Boolean(), nullable=False),
        sa.Column("is_pinned", sa.Boolean(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("video_id", "comment_id"),
    )
    op.create_index("ix_comment_published", "comments", ["video_id", "published_at"], unique=False)

    op.create_table(
        "transcripts",
        sa.Column("video_id", sa.String(length=500), nullable=False),
        sa.Column("language", sa.String(length=64), nullable=False),
        sa.Column("is_automatic", sa.Boolean(), nullable=False),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("segment_count", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("video_id", "language"),
    )

    op.create_table(
        "flatten_progress",
        sa.Column("identifier", sa.String(length=32), nullable=False),
        sa.Column("cursor_fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cursor_identifier", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("identifier"),
    )


def downgrade() -> None:
    op.drop_table("flatten_progress")
    op.drop_table("transcripts")
    op.drop_index("ix_comment_published", table_name="comments")
    op.drop_table("comments")
    op.drop_index("ix_channel_snapshot_series", table_name="channel_snapshots")
    op.drop_table("channel_snapshots")
    op.drop_index("ix_listing_entry_video", table_name="listing_entries")
    op.drop_index("ix_listing_entry_series", table_name="listing_entries")
    op.drop_table("listing_entries")
    op.drop_index("ix_video_snapshot_series", table_name="video_snapshots")
    op.drop_table("video_snapshots")
```

- [ ] **Step 4: Run the migration agreement tests**

Run: `uv run pytest tests/test_postgres_migrations.py -x -q` (needs the throwaway PostgreSQL; if the fixture skips asking for `TUBEDEPTH_TEST_POSTGRES_URL`, run `just test` instead — it brings one up).
Expected: PASS — `compare_metadata` finds no drift between the models and the migrated schema.

- [ ] **Step 5: Run the full check and commit**

Run: `just check`
Expected: green.

```bash
git add src/tubedepth/models.py migrations/versions/20260821_*_flatten_tables.py
git commit -m "feat(flatten): add flattened observation tables"
```

---

### Task 2: Pure transforms — payload dict to row dicts

**Files:**
- Create: `src/tubedepth/flatten.py` (transforms only in this task; the service lands in Task 3 in the same file)
- Create: `tests/test_flatten_transforms.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure functions over dicts).
- Produces (Task 3 and tests rely on these exact names/signatures):
  - `class FlattenError(ValueError)`
  - `@dataclass(frozen=True, slots=True) class Observation: artifact_id: str; kind: str; target: str; fetched_at: datetime`
  - `def video_snapshot_row(observation: Observation, payload: Mapping[str, Any]) -> dict[str, Any]`
  - `def listing_entry_rows(observation: Observation, payload: Mapping[str, Any]) -> list[dict[str, Any]]`
  - `def channel_snapshot_row(observation: Observation, payload: Mapping[str, Any]) -> dict[str, Any]`
  - `def comment_rows(observation: Observation, payload: Mapping[str, Any]) -> list[dict[str, Any]]`
  - `def transcript_row(observation: Observation, payload: Mapping[str, Any]) -> dict[str, Any]`

Row dicts carry exactly the model columns of Task 1 (transform output keys == `mapped_column` names). Parsing is lenient: a missing optional field becomes `None`; a list item with no id is dropped; a payload missing an essential (`video_id`, `title` for metadata; `channel_id` for about; `language`/`full_text` structure for transcript) raises `FlattenError` — the caller counts it and moves on.

- [ ] **Step 1: Write the failing tests**

`tests/test_flatten_transforms.py`:

```python
"""The payload-to-row transforms, as pure functions.

Deliberately no database and no Pydantic models here: flatten reads stored
JSON of any historical schema_version, so the transforms are exercised on
plain dicts — including dicts missing fields today's models would require.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tubedepth.flatten import (
    FlattenError,
    Observation,
    channel_snapshot_row,
    comment_rows,
    listing_entry_rows,
    transcript_row,
    video_snapshot_row,
)

FETCHED = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def observed(kind: str, target: str) -> Observation:
    return Observation(artifact_id="a" * 32, kind=kind, target=target, fetched_at=FETCHED)


class TestVideoSnapshot:
    def test_flattens_the_counts_and_identity(self) -> None:
        row = video_snapshot_row(
            observed("video.metadata", "abc123"),
            {
                "video_id": "abc123",
                "title": "A title",
                "channel": "Chan",
                "channel_id": "UC1",
                "duration_seconds": 61,
                "view_count": 1000,
                "like_count": 10,
                "comment_count": 5,
                "published_at": "2026-08-01T00:00:00+00:00",
                "published_date": "2026-08-01",
            },
        )
        assert row["artifact_id"] == "a" * 32
        assert row["video_id"] == "abc123"
        assert row["fetched_at"] == FETCHED
        assert row["view_count"] == 1000
        assert row["published_at"] == datetime(2026, 8, 1, tzinfo=UTC)
        assert row["published_date"].isoformat() == "2026-08-01"

    def test_missing_optionals_become_none(self) -> None:
        row = video_snapshot_row(
            observed("video.metadata", "abc123"),
            {"video_id": "abc123", "title": "A title"},
        )
        assert row["view_count"] is None
        assert row["published_at"] is None
        assert row["published_date"] is None

    def test_a_payload_without_a_video_id_is_refused(self) -> None:
        with pytest.raises(FlattenError):
            video_snapshot_row(observed("video.metadata", "abc123"), {"title": "A title"})

    def test_a_naive_instant_is_read_as_utc(self) -> None:
        row = video_snapshot_row(
            observed("video.metadata", "abc123"),
            {"video_id": "abc123", "title": "t", "published_at": "2026-08-01T00:00:00"},
        )
        assert row["published_at"] == datetime(2026, 8, 1, tzinfo=UTC)


class TestListingEntries:
    def test_positions_follow_list_order(self) -> None:
        rows = listing_entry_rows(
            observed("search.videos", "화장품"),
            {
                "videos": [
                    {"video_id": "v1", "title": "one", "view_count": 5},
                    {"video_id": "v2"},
                ]
            },
        )
        assert [(r["position"], r["video_id"]) for r in rows] == [(0, "v1"), (1, "v2")]
        assert rows[0]["kind"] == "search.videos"
        assert rows[0]["target"] == "화장품"
        assert rows[1]["title"] is None

    def test_an_entry_without_a_video_id_is_dropped_not_fatal(self) -> None:
        rows = listing_entry_rows(
            observed("search.videos", "q"),
            {"videos": [{"title": "placeholder"}, {"video_id": "v2"}]},
        )
        assert [r["video_id"] for r in rows] == ["v2"]
        # Positions still reflect the listing as observed, so v2 stays at 1.
        assert rows[0]["position"] == 1

    def test_an_empty_listing_is_a_real_answer(self) -> None:
        assert listing_entry_rows(observed("search.videos", "q"), {"videos": []}) == []


class TestChannelSnapshot:
    def test_flattens_the_about_panel(self) -> None:
        row = channel_snapshot_row(
            observed("channel.about", "UC1"),
            {
                "channel_id": "UC1",
                "name": "Chan",
                "handle": "@chan",
                "subscriber_count_approximate": 4530000,
                "view_count": 999,
                "video_count": 12,
                "country": "KR",
            },
        )
        assert row["channel_id"] == "UC1"
        assert row["subscriber_count_approximate"] == 4530000

    def test_a_payload_without_a_channel_id_is_refused(self) -> None:
        with pytest.raises(FlattenError):
            channel_snapshot_row(observed("channel.about", "UC1"), {"name": "Chan"})


class TestCommentRows:
    def test_video_id_comes_from_the_target(self) -> None:
        rows = comment_rows(
            observed("video.comments", "abc123"),
            {
                "comments": [
                    {
                        "comment_id": "c1",
                        "text": "hi",
                        "like_count": 2,
                        "is_pinned": True,
                        "published_at": "2026-08-20T00:00:00+00:00",
                    }
                ]
            },
        )
        (row,) = rows
        assert row["video_id"] == "abc123"
        assert row["comment_id"] == "c1"
        assert row["first_seen_at"] == FETCHED
        assert row["last_seen_at"] == FETCHED
        assert row["is_pinned"] is True
        assert row["is_hearted_by_uploader"] is False

    def test_a_comment_without_an_id_is_dropped(self) -> None:
        rows = comment_rows(
            observed("video.comments", "abc123"),
            {"comments": [{"text": "no id"}, {"comment_id": "c2", "text": "ok"}]},
        )
        assert [r["comment_id"] for r in rows] == ["c2"]

    def test_duplicate_ids_within_one_harvest_keep_the_last(self) -> None:
        rows = comment_rows(
            observed("video.comments", "abc123"),
            {
                "comments": [
                    {"comment_id": "c1", "text": "first"},
                    {"comment_id": "c1", "text": "second"},
                ]
            },
        )
        (row,) = rows
        assert row["text"] == "second"


class TestTranscriptRow:
    def test_flattens_the_transcript(self) -> None:
        row = transcript_row(
            observed("video.transcript", "abc123"),
            {"language": "ko", "is_automatic": True, "full_text": "말", "segments": [1, 2]},
        )
        assert row == {
            "video_id": "abc123",
            "language": "ko",
            "is_automatic": True,
            "full_text": "말",
            "segment_count": 2,
            "fetched_at": FETCHED,
        }

    def test_a_transcript_without_a_language_is_refused(self) -> None:
        with pytest.raises(FlattenError):
            transcript_row(observed("video.transcript", "abc123"), {"full_text": "말"})
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_flatten_transforms.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tubedepth.flatten'`.

- [ ] **Step 3: Implement the transforms**

Create `src/tubedepth/flatten.py`:

```python
"""Flattening stored payloads into queryable tables.

The artifact index deliberately keeps observations as opaque blobs; this
module is the one place that opens them for SQL. Transforms are pure
functions over plain dicts — not Pydantic models — because the store holds
every historical schema_version and the original observation is the thing
worth keeping: a payload today's model would reject still flattens as far
as its fields go.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any


class FlattenError(ValueError):
    """A payload that cannot be flattened. Counted, never fatal to a pass."""


@dataclass(frozen=True, slots=True)
class Observation:
    """The index row's identity, handed to every transform.

    The payload does not always carry its own subject (`video.comments`
    stores no video id), so the artifact's `target` travels with it.
    """

    artifact_id: str
    kind: str
    target: str
    fetched_at: datetime


def _instant(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FlattenError(f"not an instant: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise FlattenError(f"unreadable instant: {value!r}") from error
    # Stored payloads are UTC by contract; a naive one predates the contract
    # being enforced and reads as UTC rather than refusing history.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _day(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FlattenError(f"not a date: {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise FlattenError(f"unreadable date: {value!r}") from error


def _integer(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise FlattenError(f"not a count: {value!r}")
    return value


def _required_text(payload: Mapping[str, Any], field: str, kind: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise FlattenError(f"a {kind} payload has no usable {field}: {value!r}")
    return value


def video_snapshot_row(observation: Observation, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": observation.artifact_id,
        "video_id": _required_text(payload, "video_id", observation.kind),
        "fetched_at": observation.fetched_at,
        "title": _required_text(payload, "title", observation.kind),
        "channel": payload.get("channel"),
        "channel_id": payload.get("channel_id"),
        "duration_seconds": _integer(payload.get("duration_seconds")),
        "view_count": _integer(payload.get("view_count")),
        "like_count": _integer(payload.get("like_count")),
        "comment_count": _integer(payload.get("comment_count")),
        "published_at": _instant(payload.get("published_at")),
        "published_date": _day(payload.get("published_date")),
    }


def listing_entry_rows(
    observation: Observation, payload: Mapping[str, Any]
) -> list[dict[str, Any]]:
    entries = payload.get("videos")
    if not isinstance(entries, list):
        raise FlattenError(f"a {observation.kind} payload has no videos list")
    rows: list[dict[str, Any]] = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        video_id = entry.get("video_id")
        if not isinstance(video_id, str) or not video_id:
            # A placeholder for a deleted or private entry. The listing's
            # positions stay as observed; the row is simply absent.
            continue
        rows.append(
            {
                "artifact_id": observation.artifact_id,
                "position": position,
                "kind": observation.kind,
                "target": observation.target,
                "fetched_at": observation.fetched_at,
                "video_id": video_id,
                "title": entry.get("title"),
                "view_count": _integer(entry.get("view_count")),
                "duration_seconds": _integer(entry.get("duration_seconds")),
                "channel": entry.get("channel"),
                "channel_id": entry.get("channel_id"),
                "published_at": _instant(entry.get("published_at")),
            }
        )
    return rows


def channel_snapshot_row(observation: Observation, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": observation.artifact_id,
        "channel_id": _required_text(payload, "channel_id", observation.kind),
        "fetched_at": observation.fetched_at,
        "name": payload.get("name"),
        "handle": payload.get("handle"),
        "subscriber_count_approximate": _integer(payload.get("subscriber_count_approximate")),
        "view_count": _integer(payload.get("view_count")),
        "video_count": _integer(payload.get("video_count")),
        "country": payload.get("country"),
    }


def comment_rows(observation: Observation, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    comments = payload.get("comments")
    if not isinstance(comments, list):
        raise FlattenError(f"a {observation.kind} payload has no comments list")
    # Keyed to drop in-payload duplicates: one ON CONFLICT statement cannot
    # touch the same row twice, and the last occurrence is the harvester's
    # final word.
    rows: dict[str, dict[str, Any]] = {}
    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        comment_id = comment.get("comment_id")
        if not isinstance(comment_id, str) or not comment_id:
            continue
        text = comment.get("text")
        rows[comment_id] = {
            "video_id": observation.target,
            "comment_id": comment_id,
            "parent_id": comment.get("parent_id"),
            "text": text if isinstance(text, str) else "",
            "author": comment.get("author"),
            "author_id": comment.get("author_id"),
            "like_count": _integer(comment.get("like_count")),
            "is_hearted_by_uploader": bool(comment.get("is_hearted_by_uploader", False)),
            "is_pinned": bool(comment.get("is_pinned", False)),
            "published_at": _instant(comment.get("published_at")),
            "first_seen_at": observation.fetched_at,
            "last_seen_at": observation.fetched_at,
        }
    return list(rows.values())


def transcript_row(observation: Observation, payload: Mapping[str, Any]) -> dict[str, Any]:
    segments = payload.get("segments")
    return {
        "video_id": observation.target,
        "language": _required_text(payload, "language", observation.kind),
        "is_automatic": bool(payload.get("is_automatic", False)),
        "full_text": payload.get("full_text") or "",
        "segment_count": len(segments) if isinstance(segments, list) else 0,
        "fetched_at": observation.fetched_at,
    }
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_flatten_transforms.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tubedepth/flatten.py tests/test_flatten_transforms.py
git commit -m "feat(flatten): pure payload-to-row transforms"
```

---

### Task 3: FlattenService — cursor walk, routing, upserts

**Files:**
- Modify: `src/tubedepth/flatten.py` (append the service below the transforms)
- Test: `tests/test_flatten_service.py`

**Interfaces:**
- Consumes: Task 1 models (exact names above), Task 2 transforms, `Database` (`.session()` context manager — read `src/tubedepth/database.py:298` for its commit/rollback semantics before writing code), `PayloadStore.read(digest) -> bytes` (raises `FileNotFoundError` when the blob is gone), `utcnow`.
- Produces (Task 4 relies on): `FlattenService(database=..., payloads=..., clock=utcnow)` with method `run(*, batch_size: int = 200, limit: int | None = None, dry_run: bool = False) -> FlattenOutcome`; `FlattenOutcome` fields `artifacts_seen: int`, `flattened: dict[str, int]` (kind → artifacts flattened), `skipped_unhandled: int`, `skipped_missing_payload: int`, `errors: int`, `cursor_fetched_at: datetime | None`.

Behaviour to implement (each point is asserted by a test in Step 1):

1. Walk `artifacts` in ascending `(fetched_at, identifier)` order, strictly after the stored cursor, and only rows with `fetched_at < clock() - 5 minutes` (`SAFETY_LAG = timedelta(minutes=5)`). Use `sa.tuple_(Artifact.fetched_at, Artifact.identifier) > sa.tuple_(...)` for the cursor comparison.
2. Route by kind: `video.metadata` → `video_snapshot_row`; `search.videos`/`channel.videos`/`playlist.items`/`trending.videos` → `listing_entry_rows`; `channel.about` → `channel_snapshot_row`; `video.comments` → `comment_rows`; `video.transcript` → `transcript_row`; `video.bundle` → decode `payload["parts"]` (a dict keyed by kind) and route each part through the same handlers with the bundle's `Observation` (same artifact_id, fetched_at; target = bundle target). Any other kind counts as `skipped_unhandled`.
3. Upserts, one statement per artifact (PostgreSQL `insert(...).on_conflict_*` from `sqlalchemy.dialects.postgresql`):
   - `video_snapshots`, `listing_entries`, `channel_snapshots`: `on_conflict_do_nothing` on the primary key.
   - `comments`: `on_conflict_do_update(index_elements=["video_id", "comment_id"], where=excluded.last_seen_at > CommentRecord.last_seen_at, set_={text, author, author_id, like_count, is_hearted_by_uploader, is_pinned, published_at, last_seen_at ← excluded})`. `first_seen_at` is never in `set_` — ascending processing order makes the first insert the earliest observation.
   - `transcripts`: `on_conflict_do_update(index_elements=["video_id", "language"], where=excluded.fetched_at > TranscriptRecord.fetched_at, set_=everything else ← excluded)`.
4. `payloads.read(digest)` raising `FileNotFoundError` → count `skipped_missing_payload`, continue. `FlattenError` (or `json.JSONDecodeError`) from decoding/transforming → count `errors`, log at warning with the artifact id, continue. Either way the cursor still advances past the artifact.
5. One transaction per batch: all upserts plus the `FlattenProgress` upsert commit together. `dry_run=True` performs the reads and transforms, rolls the transaction back, and never writes the cursor — but still advances an in-memory cursor so the loop terminates. The outcome's counts are identical to what a real run would report.
6. `limit` bounds total artifacts examined across batches; `batch_size` bounds one transaction.
7. An empty batch ends the run. `cursor_fetched_at` in the outcome is the final cursor position (or `None` if nothing was ever processed and no cursor row exists).

- [ ] **Step 1: Write the failing tests**

`tests/test_flatten_service.py` — use the `database` fixture from `conftest.py` (a migrated throwaway schema; read how `tests/test_artifact_cache.py` builds sessions and stores payloads, and mirror it). Helpers: a `store(kind, target, payload_dict, fetched_at)` function that gzips the JSON via `PayloadStore.put` and inserts an `Artifact` row with chosen `fetched_at`; a fixed `clock` far in the future so the safety lag never filters test rows. Cover, at minimum:

```python
def test_a_metadata_artifact_becomes_one_snapshot_row(...): ...
def test_running_twice_adds_no_rows(...): ...          # same artifacts, run(), run(), counts equal
def test_a_listing_fans_out_to_entry_rows(...): ...
def test_a_bundle_routes_its_parts(...): ...           # parts: video.metadata + video.comments
def test_comments_deduplicate_across_harvests(...): ...  # two harvests, same comment_id, newer text/like_count wins, first_seen from the first
def test_an_older_replay_does_not_regress_a_comment(...): ...  # after both harvests, re-run from a reset cursor; text stays the newest
def test_transcripts_keep_the_newest(...): ...
def test_a_missing_payload_is_skipped_and_the_cursor_passes_it(...): ...
def test_an_unreadable_payload_counts_as_an_error_not_a_crash(...): ...  # store raw junk bytes
def test_an_unhandled_kind_is_counted(...): ...        # e.g. video.related
def test_the_cursor_resumes_where_it_stopped(...): ...  # run(limit=1) then run(); second run starts after the first artifact
def test_the_safety_lag_holds_back_fresh_artifacts(...): ...  # fetched_at now: not processed; clock advanced: processed
def test_dry_run_writes_nothing(...): ...              # counts reported, tables empty, no cursor row
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_flatten_service.py -q`
Expected: FAIL with `ImportError` (no `FlattenService`).

- [ ] **Step 3: Implement `FlattenService`**

Append to `src/tubedepth/flatten.py` per the behaviour list above. Keep the kind→handler routing in one module-level dict (`_HANDLERS`), the per-table upsert builders as small private functions, and the batch loop free of transform knowledge. Match the service style of `retention.py` (frozen dataclass config where sensible, `logger = logging.getLogger(__name__)`).

- [ ] **Step 4: Run the tests, then the whole suite**

Run: `uv run pytest tests/test_flatten_service.py -q`, then `just check`.
Expected: PASS / green.

- [ ] **Step 5: Commit**

```bash
git add src/tubedepth/flatten.py tests/test_flatten_service.py
git commit -m "feat(flatten): incremental cursor-driven flatten service"
```

---

### Task 4: The `tubedepth flatten` command

**Files:**
- Modify: `src/tubedepth/cli.py` (new command beside `prune`)
- Test: `tests/test_cli.py` (append tests; reuse this file's `_cli_database_url` autouse fixture and helpers)

**Interfaces:**
- Consumes: `FlattenService`, `FlattenOutcome` from Task 3; `_database`, `_payload_store`, `_stopping_on_signals`, `configure_logging` already in `cli.py`.
- Produces: the `flatten` subcommand with options `--data-dir`, `--batch` (default 200), `--limit` (default none), `--dry-run`, `--every` (seconds, envvar `TUBEDEPTH_FLATTEN_SECONDS`, 0 = once) — `deploy/` units in Task 5 name these exact options.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py` (mirroring the file's existing style and fixtures):

```python
class TestFlatten:
    def test_flattens_what_the_worker_stored(self, tmp_path: Path) -> None:
        # Arrange one video.metadata artifact + payload via the same helpers
        # the artifact-cache tests use, then:
        result = runner.invoke(application, ["flatten", "--data-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "flattened" in result.output
        # Assert one row in video_snapshots via a direct engine query.

    def test_dry_run_reports_without_writing(
        self, tmp_path: Path
    ) -> None: ...  # exit 0, table still empty

    def test_a_second_pass_is_a_no_op(
        self, tmp_path: Path
    ) -> None: ...  # run twice; second output reports 0 artifacts seen
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_cli.py -k Flatten -q`
Expected: FAIL — `flatten` is not a command.

- [ ] **Step 3: Implement the command**

In `src/tubedepth/cli.py`, next to `prune`:

```python
@application.command()
def flatten(
    data_directory: Annotated[Path, typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR")] = Path(
        "var"
    ),
    batch: Annotated[int, typer.Option("--batch", help="Artifacts per transaction")] = 200,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Stop after this many artifacts")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what a pass would do, write nothing")
    ] = False,
    every: Annotated[
        float,
        typer.Option(
            "--every",
            envvar="TUBEDEPTH_FLATTEN_SECONDS",
            help="Stay up and flatten again this often, in seconds. 0 runs once and exits.",
        ),
    ] = 0.0,
) -> None:
    """Unpack stored payloads into the queryable tables, incrementally.

    The artifact index keeps observations as blobs on disk, which PostgREST
    cannot see into; this walks everything new since the last pass and
    upserts the flattened rows. Idempotent by construction — the tables'
    keys make a replayed artifact a no-op — so rerunning after a crash is
    the recovery procedure, not a hazard.

    Without `--every` this runs one pass and exits, which is what a timer
    wants and how `deploy/tubedepth-flatten.timer` runs it. `--every` is for
    the environments with no scheduler, compose among them.
    """
    configure_logging()
    service = FlattenService(
        database=_database(data_directory), payloads=_payload_store(data_directory)
    )

    def sweep() -> None:
        outcome = service.run(batch_size=batch, limit=limit, dry_run=dry_run)
        flattened = sum(outcome.flattened.values())
        prefix = "would flatten" if dry_run else "flattened"
        typer.echo(
            f"✓ {prefix} {flattened} of {outcome.artifacts_seen} artifact(s)"
            + "".join(f"\n  {kind}: {count}" for kind, count in sorted(outcome.flattened.items()))
        )
        if outcome.skipped_unhandled:
            typer.echo(f"  skipped {outcome.skipped_unhandled} artifact(s) of unhandled kinds")
        if outcome.skipped_missing_payload:
            typer.echo(
                f"  skipped {outcome.skipped_missing_payload} artifact(s) whose payload is gone"
            )
        if outcome.errors:
            typer.echo(f"  {outcome.errors} payload(s) would not flatten — see the log", err=True)

    if every <= 0:
        sweep()
        return

    stop = _stopping_on_signals()
    sweep()
    while not stop.wait(every):
        try:
            sweep()
        except Exception:
            # Deliberately everything, matching `watch`: after the first
            # pass, staying up and complaining beats exiting and flattening
            # nothing.
            logger.exception("a flatten pass failed; retried on the next interval")
```

Add the import `from .flatten import FlattenService` beside the other service imports.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_cli.py -k Flatten -q`, then `just check`.
Expected: PASS / green.

- [ ] **Step 5: Commit**

```bash
git add src/tubedepth/cli.py tests/test_cli.py
git commit -m "feat(flatten): tubedepth flatten command"
```

---

### Task 5: Deployment units, compose service, connection budget

**Files:**
- Create: `deploy/tubedepth-flatten.service`, `deploy/tubedepth-flatten.timer`
- Modify: `deploy/docker-compose.yml` (new `tubedepth-flatten` service)
- Modify: `service-db.json` (`workers_and_schedulers` 0 → 1, `service_spare` 7 → 6; total stays 32)
- Test: existing `tests/test_deployment_units.py`, `tests/test_compose.py` (parameterized discovery picks the new unit up automatically — read both test files first; they are the requirements for these files)

**Interfaces:**
- Consumes: the `flatten` command and its exact option names from Task 4.
- Produces: reference deployment the human mirrors into `../stack` (out of scope here).

- [ ] **Step 1: Write the units**

`deploy/tubedepth-flatten.service` — copy the structure of `tubedepth-watch.service` (same PATH line, same EnvironmentFile, same sandboxing block, same `uv run --frozen`), with:

```ini
[Unit]
Description=tubedepth flatten: unpack new payloads into the queryable tables
Documentation=file://%h/github_prj/yt-scrapper/docs/superpowers/specs/2026-08-21-flatten-etl-design.md
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/github_prj/yt-scrapper
Environment=PATH=%h/.nix-profile/bin:/usr/local/bin:/usr/bin:/bin
Environment=TUBEDEPTH_DATA_DIR=%h/github_prj/yt-scrapper/var
EnvironmentFile=%h/.config/tubedepth/worker.env
ExecStart=/usr/bin/env uv run --frozen tubedepth flatten
```

plus the full sandbox block copied from the watch unit, and a top comment (same voice as the other units) explaining: one-shot because systemd already has a scheduler; idempotent so a crashed pass needs no cleanup; reads and writes the database plus reads the payload store, does no extraction.

`deploy/tubedepth-flatten.timer`:

```ini
[Unit]
Description=Flatten new payloads every 15 minutes

[Timer]
OnBootSec=7min
OnUnitActiveSec=15min
Persistent=true
RandomizedDelaySec=1min

[Install]
WantedBy=timers.target
```

with a comment: 15 minutes keeps the tables at most one trending-TTL behind; the pass is database-local (no YouTube traffic), so cadence costs nothing outside the shared PostgreSQL.

- [ ] **Step 2: Add the compose service**

In `deploy/docker-compose.yml`, next to the watch service (mirror its shape — anchor env, volume, depends_on migrate; compose has no timers, so it stays resident):

```yaml
  tubedepth-flatten:
    image: tubedepth
    command: flatten --every 900
    environment: *tubedepth-environment
    volumes:
      - tubedepth-data:/var/lib/tubedepth
    depends_on:
      tubedepth-migrate:
        condition: service_completed_successfully
    restart: unless-stopped
```

Match the existing file's exact volume name/user/network conventions — read the watch service entry and copy its shape, changing only the command.

- [ ] **Step 3: Update the budget**

In `service-db.json`: `"workers_and_schedulers": 1`, `"service_spare": 6`. If `tests/test_deployment_units.py::test_the_connection_budget_agrees_everywhere_it_is_declared` disagrees with this split, read the test — it documents the formula — and adjust so the declared total still balances; do not change the total.

- [ ] **Step 4: Run the deployment tests**

Run: `uv run pytest tests/test_deployment_units.py tests/test_compose.py -q`
Expected: PASS — the parameterized tests discover the new unit/service and hold it to the same rules (command exists, options exist, lock pinned, sandbox writable paths, timer↔service pairing, no secrets).

- [ ] **Step 5: Run the full check and commit**

Run: `just check`
Expected: green.

```bash
git add deploy/tubedepth-flatten.service deploy/tubedepth-flatten.timer deploy/docker-compose.yml service-db.json
git commit -m "feat(flatten): deployment units, compose service and budget"
```

---

### Task 6: Documentation

**Files:**
- Modify: `docs/status.md` (decision record, Korean)
- Modify: `CHANGELOG.md` and `CHANGELOG.ko.md` (Unreleased)
- Modify: `README.md` / `README.ko.md` only if they enumerate CLI commands (check first: `grep -n 'watch\|prune' README.md`)
- Test: `uv run pytest tests/test_documentation_is_true.py -q`

**Interfaces:**
- Consumes: everything shipped in Tasks 1–5.
- Produces: the record future sessions read.

- [ ] **Step 1: status.md decision record**

Append a dated section to `docs/status.md` (Korean, matching the document's voice) covering: why the flattened tables carry no FK to `artifacts` (retention deletes artifact rows; the flattened series is the part meant to outlive the blobs); why the cursor has a 5-minute safety lag (concurrent commits can land out of `fetched_at` order; the lag plus idempotent upserts make the walk safe); why comment/transcript upserts are gated on observation recency (a replayed old blob must not regress the newest observation); and that PostgREST exposure needed nothing here (bootstrap default privileges + stack grant script). Note explicitly that enabling `tubedepth-flatten.timer` (and mirroring the compose service into `../stack`) is the operator's step.

- [ ] **Step 2: CHANGELOG**

Under `Unreleased` in `CHANGELOG.md` (English) and `CHANGELOG.ko.md` (Korean), add: `tubedepth flatten` — incremental ETL unpacking stored payloads into six queryable tables (`video_snapshots`, `listing_entries`, `channel_snapshots`, `comments`, `transcripts`, `flatten_progress`) for PostgREST/data-portal; plus the deployment units. Follow the file's existing entry format exactly.

- [ ] **Step 3: README, only if it lists commands**

If `README.md` has a command list mentioning `watch`/`prune`, add `flatten` in the same style to both language versions. If it does not, skip — do not invent a new section.

- [ ] **Step 4: Run the documentation test and the full check**

Run: `uv run pytest tests/test_documentation_is_true.py -q`, then `just check`.
Expected: PASS / green.

- [ ] **Step 5: Commit**

```bash
git add docs/status.md CHANGELOG.md CHANGELOG.ko.md README.md README.ko.md
git commit -m "docs(flatten): record the flatten ETL decisions"
```
