"""Video detail: the fields the official Data API does not expose."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ..schemas import Chapter, ReplaySegment, VideoMetadata


def _most_replayed(heatmap: Sequence[Mapping[str, Any]] | None) -> list[ReplaySegment]:
    ordered = sorted(heatmap or [], key=lambda bucket: bucket["value"], reverse=True)
    return [
        ReplaySegment(
            rank=position,
            start_seconds=bucket["start_time"],
            end_seconds=bucket["end_time"],
            score=bucket["value"],
        )
        for position, bucket in enumerate(ordered, start=1)
    ]


def _chapters(chapters: Sequence[Mapping[str, Any]] | None) -> list[Chapter]:
    return [
        Chapter(
            title=chapter["title"],
            start_seconds=chapter["start_time"],
            end_seconds=chapter.get("end_time"),
        )
        for chapter in chapters or []
    ]


def _published_at(timestamp: int | None) -> datetime | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC)


def normalize(dump: Mapping[str, Any]) -> VideoMetadata:
    """Turn one yt-dlp dump into the public contract."""
    return VideoMetadata(
        video_id=dump["id"],
        title=dump["title"],
        channel_id=dump.get("channel_id"),
        tags=list(dump.get("tags") or []),
        published_at=_published_at(dump.get("timestamp")),
        chapters=_chapters(dump.get("chapters")),
        most_replayed=_most_replayed(dump.get("heatmap")),
    )
