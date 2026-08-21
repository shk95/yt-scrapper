"""Flattening stored payloads into queryable tables.

The artifact index deliberately keeps observations as opaque blobs; this
module is the one place that opens them for SQL. Transforms are pure
functions over plain dicts — not Pydantic models — because the store holds
every historical schema_version, and the original observation is the thing
worth keeping: a payload today's model would reject still flattens as far
as its fields go.

`FlattenService` below is the only caller: a cursor walk over `artifacts`
that reads each payload once, routes it to a transform by kind, and upserts
the rows. It knows nothing about payload shapes, and the transforms know
nothing about SQL.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, String, literal, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql import Executable

from .database import Database
from .models import (
    FLATTEN_PROGRESS_ID,
    Artifact,
    ChannelSnapshot,
    CommentRecord,
    FlattenProgress,
    ListingEntry,
    TranscriptRecord,
    UtcDateTime,
    VideoSnapshot,
    utcnow,
)
from .payload_store import PayloadStore

logger = logging.getLogger(__name__)

# How far behind the clock the walk stays. An artifact row is committed after
# its payload is written, but the two are not one transaction, and a row read
# the instant it appears can point at a blob whose write has not landed. Five
# minutes is far longer than that gap and far shorter than the collection
# intervals, so the cursor is never both behind and stuck.
SAFETY_LAG = timedelta(minutes=5)


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


def _required_text(payload: Mapping[str, Any], name: str, kind: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise FlattenError(f"a {kind} payload has no usable {name}: {value!r}")
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


Rows = list[dict[str, Any]]


def _video_snapshots(observation: Observation, payload: Mapping[str, Any]) -> Rows:
    return [video_snapshot_row(observation, payload)]


def _channel_snapshots(observation: Observation, payload: Mapping[str, Any]) -> Rows:
    return [channel_snapshot_row(observation, payload)]


def _transcripts(observation: Observation, payload: Mapping[str, Any]) -> Rows:
    return [transcript_row(observation, payload)]


def _upsert_video_snapshots(rows: Rows) -> Executable:
    # The artifact id is the primary key, so re-reading an artifact — a
    # replay, a cleared cursor — writes the same row it wrote before. Nothing
    # about a past observation can change, so there is nothing to update.
    return insert(VideoSnapshot).values(rows).on_conflict_do_nothing(index_elements=["artifact_id"])


def _upsert_listing_entries(rows: Rows) -> Executable:
    return (
        insert(ListingEntry)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["artifact_id", "position"])
    )


def _upsert_channel_snapshots(rows: Rows) -> Executable:
    return (
        insert(ChannelSnapshot).values(rows).on_conflict_do_nothing(index_elements=["artifact_id"])
    )


def _upsert_comments(rows: Rows) -> Executable:
    """One row per (video, comment), following the newest harvest that saw it.

    The `where` is what makes a replay safe. Harvests overlap and the walk is
    ascending, but a cleared cursor re-reads old observations after new ones
    have landed — without the guard, yesterday's wording would overwrite
    today's edit. `first_seen_at` is never in `set_` for the same reason from
    the other end: the first insert is the earliest observation of that
    comment, and every later statement leaves it alone.
    """
    statement = insert(CommentRecord).values(rows)
    excluded = statement.excluded
    return statement.on_conflict_do_update(
        index_elements=["video_id", "comment_id"],
        where=excluded.last_seen_at > CommentRecord.last_seen_at,
        set_={
            "text": excluded.text,
            "author": excluded.author,
            "author_id": excluded.author_id,
            "like_count": excluded.like_count,
            "is_hearted_by_uploader": excluded.is_hearted_by_uploader,
            "is_pinned": excluded.is_pinned,
            "published_at": excluded.published_at,
            "last_seen_at": excluded.last_seen_at,
        },
    )


def _upsert_transcripts(rows: Rows) -> Executable:
    """The newest transcript per (video, language), and only forwards."""
    statement = insert(TranscriptRecord).values(rows)
    excluded = statement.excluded
    return statement.on_conflict_do_update(
        index_elements=["video_id", "language"],
        where=excluded.fetched_at > TranscriptRecord.fetched_at,
        set_={
            "is_automatic": excluded.is_automatic,
            "full_text": excluded.full_text,
            "segment_count": excluded.segment_count,
            "fetched_at": excluded.fetched_at,
        },
    )


@dataclass(frozen=True, slots=True)
class _Handler:
    transform: Callable[[Observation, Mapping[str, Any]], Rows]
    upsert: Callable[[Rows], Executable]


# The whole of the routing table. A new flattenable kind is a line here.
_HANDLERS: dict[str, _Handler] = {
    "video.metadata": _Handler(_video_snapshots, _upsert_video_snapshots),
    "search.videos": _Handler(listing_entry_rows, _upsert_listing_entries),
    "channel.videos": _Handler(listing_entry_rows, _upsert_listing_entries),
    "playlist.items": _Handler(listing_entry_rows, _upsert_listing_entries),
    "trending.videos": _Handler(listing_entry_rows, _upsert_listing_entries),
    "channel.about": _Handler(_channel_snapshots, _upsert_channel_snapshots),
    "video.comments": _Handler(comment_rows, _upsert_comments),
    "video.transcript": _Handler(_transcripts, _upsert_transcripts),
}

# A bundle is not a payload shape of its own: it is several of the above in
# one artifact, under `parts`, keyed by the kind each part would have had.
BUNDLE_KIND = "video.bundle"


@dataclass(frozen=True, slots=True)
class FlattenOutcome:
    """What one pass did.

    `artifacts_seen` is the total; every artifact examined is counted under
    exactly one of the others, except a bundle, which can be flattened and
    still carry a part of a kind this does not handle.
    """

    artifacts_seen: int
    # kind → how many artifacts of that kind were flattened. A bundle counts
    # once, under `video.bundle`, because one artifact was read.
    flattened: dict[str, int] = field(default_factory=dict)
    skipped_unhandled: int = 0
    skipped_missing_payload: int = 0
    errors: int = 0
    # Where the walk finished, which is what the next pass resumes from.
    cursor_fetched_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _Flattened:
    """One artifact's worth of work: what to execute, and what to count."""

    statements: list[Executable]
    handled: bool
    unhandled_parts: int


class FlattenService:
    """Walks new artifacts once and writes their rows.

    Incremental by cursor rather than by rescanning: the index is the whole
    history of collection, so a pass that re-reads it grows with the archive
    instead of with what arrived. The cursor is `(fetched_at, identifier)` —
    `fetched_at` alone is not a total order, and a cursor that cannot break a
    tie either re-reads an artifact or steps over one.

    Nothing here is fatal per artifact. A payload retention has already
    deleted, a payload that predates a normalizer and no longer parses, a
    kind this does not flatten: each is counted and the cursor moves past it.
    A pass that stopped on the first unreadable blob would stop for good.
    """

    def __init__(
        self,
        *,
        database: Database,
        payloads: PayloadStore,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._database = database
        self._payloads = payloads
        self._clock = clock

    def _stored_cursor(self) -> tuple[datetime, str] | None:
        with self._database.session(readonly=True) as session:
            progress = session.get(FlattenProgress, FLATTEN_PROGRESS_ID)
            if progress is None:
                return None
            return progress.cursor_fetched_at, progress.cursor_identifier

    def _batch_query(
        self, cursor: tuple[datetime, str] | None, size: int
    ) -> Select[tuple[Artifact]]:
        statement = select(Artifact).where(Artifact.fetched_at < self._clock() - SAFETY_LAG)
        if cursor is not None:
            # Row-wise comparison, so the pair is one ordered value rather
            # than two conditions that would have to be spelled
            # `fetched_at > x OR (fetched_at = x AND identifier > y)`. The
            # literals carry their column types: `UtcDateTime` is what makes
            # the bound instant comparable to the stored one.
            statement = statement.where(
                tuple_(Artifact.fetched_at, Artifact.identifier)
                > tuple_(literal(cursor[0], UtcDateTime), literal(cursor[1], String))
            )
        return statement.order_by(Artifact.fetched_at, Artifact.identifier).limit(size)

    def _payload(self, artifact: Artifact) -> Mapping[str, Any]:
        decoded = json.loads(self._payloads.read(artifact.digest))
        if not isinstance(decoded, Mapping):
            raise FlattenError(f"a {artifact.kind} payload is not an object")
        return decoded

    def _route(
        self, observation: Observation, payload: Mapping[str, Any], statements: list[Executable]
    ) -> bool:
        """Append one kind's upsert, or say the kind is not flattened here."""
        handler = _HANDLERS.get(observation.kind)
        if handler is None:
            return False
        rows = handler.transform(observation, payload)
        # An empty listing or an empty harvest is a real observation with
        # nothing to write; `insert().values([])` is not a statement.
        if rows:
            statements.append(handler.upsert(rows))
        return True

    def _flatten(self, artifact: Artifact) -> _Flattened:
        """Turn one artifact into statements. Raises what the caller counts."""
        observation = Observation(
            artifact_id=artifact.identifier,
            kind=artifact.kind,
            target=artifact.target,
            fetched_at=artifact.fetched_at,
        )
        payload = self._payload(artifact)
        statements: list[Executable] = []

        if artifact.kind != BUNDLE_KIND:
            return _Flattened(statements, self._route(observation, payload, statements), 0)

        parts = payload.get("parts")
        if not isinstance(parts, Mapping):
            raise FlattenError(f"a {BUNDLE_KIND} payload has no parts object")
        handled = False
        unhandled = 0
        for part_kind, part in parts.items():
            if not isinstance(part, Mapping):
                raise FlattenError(f"a {BUNDLE_KIND} part {part_kind!r} is not an object")
            # The bundle's identity, with the part's kind: the rows belong to
            # the artifact that was collected, and there is only one of it.
            part_observation = Observation(
                artifact_id=artifact.identifier,
                kind=part_kind,
                target=artifact.target,
                fetched_at=artifact.fetched_at,
            )
            if self._route(part_observation, part, statements):
                handled = True
            else:
                unhandled += 1
        return _Flattened(statements, handled, unhandled)

    def _write_cursor(self, session: Any, cursor: tuple[datetime, str]) -> None:
        fetched_at, identifier = cursor
        statement = insert(FlattenProgress).values(
            identifier=FLATTEN_PROGRESS_ID,
            cursor_fetched_at=fetched_at,
            cursor_identifier=identifier,
            updated_at=self._clock(),
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["identifier"],
                set_={
                    "cursor_fetched_at": statement.excluded.cursor_fetched_at,
                    "cursor_identifier": statement.excluded.cursor_identifier,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )

    def run(
        self, *, batch_size: int = 200, limit: int | None = None, dry_run: bool = False
    ) -> FlattenOutcome:
        """Flatten everything settled since the last pass.

        One transaction per batch: the rows and the cursor that says they
        were written commit together, so an interrupted pass resumes at an
        artifact it has not already flattened rather than at one it has.

        `dry_run` does the reads and the transforms and rolls the batch back,
        reporting the counts a real run would report. Its cursor lives in
        memory only — without that the loop would re-read the same batch for
        ever, and with it written the rehearsal would silently skip the work.
        """
        cursor = self._stored_cursor()
        seen = 0
        flattened: dict[str, int] = {}
        unhandled = 0
        missing = 0
        errors = 0

        while limit is None or seen < limit:
            size = batch_size if limit is None else min(batch_size, limit - seen)
            with self._database.session() as session:
                artifacts = list(session.scalars(self._batch_query(cursor, size)))
                if not artifacts:
                    break
                # The batch commits as one, so its cursor is the last row in
                # it — and it is read here, before anything can expire the
                # instances (`dry_run` rolls this session back below).
                cursor = (artifacts[-1].fetched_at, artifacts[-1].identifier)
                for artifact in artifacts:
                    seen += 1
                    try:
                        work = self._flatten(artifact)
                    except FileNotFoundError:
                        # Retention deletes payloads on schedule; an artifact
                        # row outliving its blob is that policy working.
                        missing += 1
                        continue
                    except (FlattenError, json.JSONDecodeError, UnicodeDecodeError) as error:
                        logger.warning(
                            "artifact %s (%s) does not flatten: %s",
                            artifact.identifier,
                            artifact.kind,
                            error,
                        )
                        errors += 1
                        continue
                    for statement in work.statements:
                        session.execute(statement)
                    if work.handled:
                        flattened[artifact.kind] = flattened.get(artifact.kind, 0) + 1
                    unhandled += work.unhandled_parts
                    if not work.handled and not work.unhandled_parts:
                        unhandled += 1
                if dry_run:
                    session.rollback()
                else:
                    self._write_cursor(session, cursor)

        if flattened or errors or missing:
            logger.info(
                "flattened %s artifact(s) of %s examined (%s missing payload, %s unflattenable)",
                sum(flattened.values()),
                seen,
                missing,
                errors,
            )

        return FlattenOutcome(
            artifacts_seen=seen,
            flattened=flattened,
            skipped_unhandled=unhandled,
            skipped_missing_payload=missing,
            errors=errors,
            cursor_fetched_at=cursor[0] if cursor else None,
        )
