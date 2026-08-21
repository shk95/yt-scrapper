"""Flattening stored payloads into queryable tables.

The artifact index deliberately keeps observations as opaque blobs; this
module is the one place that opens them for SQL. Transforms are pure
functions over plain dicts — not Pydantic models — because the store holds
every historical schema_version, and the original observation is the thing
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
