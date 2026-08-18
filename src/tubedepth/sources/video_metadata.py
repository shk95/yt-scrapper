"""Video detail: the fields the official Data API does not expose."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from ..egress.control import Lane
from ..egress.transport import Egress
from ..identifiers import TargetType
from ..schemas import CaptionTrackReference, Chapter, ReplaySegment, VideoMetadata
from .registry import SourceCost
from .ytdlp_runtime import YtdlpRuntime


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


def _caption_tracks(dump: Mapping[str, Any]) -> list[CaptionTrackReference]:
    tracks: list[CaptionTrackReference] = []
    for bucket, is_automatic in (("subtitles", False), ("automatic_captions", True)):
        for language, entries in (dump.get(bucket) or {}).items():
            name = next((entry.get("name") for entry in entries if entry.get("name")), None)
            tracks.append(
                CaptionTrackReference(language=language, name=name, is_automatic=is_automatic)
            )
    return tracks


def _published_date(upload_date: str | None) -> date | None:
    """Parse yt-dlp's YYYYMMDD upload date.

    Kept even when the exact instant is present, because the instant is the
    field that disappears: on 2026-08-18 live extractions stopped carrying it
    while this stayed.
    """
    if not upload_date:
        return None
    try:
        return datetime.strptime(upload_date, "%Y%m%d").date()
    except ValueError:
        return None


def normalize(dump: Mapping[str, Any]) -> VideoMetadata:
    """Turn one yt-dlp dump into the public contract."""
    return VideoMetadata(
        video_id=dump["id"],
        title=dump["title"],
        description=dump.get("description") or "",
        channel=dump.get("channel"),
        channel_id=dump.get("channel_id"),
        duration_seconds=dump.get("duration"),
        categories=list(dump.get("categories") or []),
        view_count=dump.get("view_count"),
        like_count=dump.get("like_count"),
        comment_count=dump.get("comment_count"),
        caption_tracks=_caption_tracks(dump),
        tags=list(dump.get("tags") or []),
        published_at=_published_at(dump.get("timestamp")),
        published_date=_published_date(dump.get("upload_date")),
        chapters=_chapters(dump.get("chapters")),
        most_replayed=_most_replayed(dump.get("heatmap")),
    )


class VideoMetadataSource:
    """Chapters, the most-replayed graph, tags, and the exact upload instant."""

    kind = "video.metadata"
    target_type = TargetType.VIDEO
    lane = Lane.YOUTUBE
    cost = SourceCost.STANDARD

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> VideoMetadata:
        return normalize(runtime.extract(target, egress=egress))
