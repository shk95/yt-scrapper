"""Enumerating videos: channels, searches and playlists.

All three come back from yt-dlp in the same shape — a playlist with entries —
so one normalizer serves them and the sources differ only in how they build
the target they hand to the extractor.

These are the input side of large-scale collection. Without them every video
id has to be typed by hand, which stops volume well before throughput does.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ..egress.transport import Egress
from ..identifiers import TargetType
from ..schemas import ListedVideo, VideoListing
from .ytdlp_runtime import YtdlpRuntime

# `extract_flat` is what makes a listing one request instead of one per video.
FLAT_OPTIONS: dict[str, Any] = {"extract_flat": "in_playlist"}

DEFAULT_LIMIT = 100
VIDEO_IDENTIFIER_LENGTH = 11


def _published_at(timestamp: int | None) -> datetime | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC)


def normalize(dump: Mapping[str, Any], *, source_kind: str) -> VideoListing:
    videos: list[ListedVideo] = []
    skipped = 0

    for entry in dump.get("entries") or []:
        identifier = entry.get("id")
        # yt-dlp leaves a placeholder for deleted and private videos. Carrying
        # one through would queue a job that can only ever fail.
        if not identifier or len(identifier) != VIDEO_IDENTIFIER_LENGTH:
            skipped += 1
            continue
        videos.append(
            ListedVideo(
                video_id=identifier,
                title=entry.get("title"),
                duration_seconds=int(entry["duration"]) if entry.get("duration") else None,
                view_count=entry.get("view_count"),
                channel=entry.get("channel") or entry.get("uploader"),
                channel_id=entry.get("channel_id"),
                published_at=_published_at(entry.get("timestamp")),
            )
        )

    return VideoListing(
        source_kind=source_kind,
        listing_id=dump.get("id"),
        title=dump.get("title"),
        videos=videos,
        skipped_count=skipped,
    )


class _ListingSource:
    kind: str
    target_type: TargetType

    def __init__(self, *, limit: int = DEFAULT_LIMIT) -> None:
        self._limit = limit

    def _extraction_target(self, target: str) -> str:
        raise NotImplementedError

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> VideoListing:
        dump = runtime.extract(
            self._extraction_target(target),
            egress=egress,
            options={**FLAT_OPTIONS, "playlistend": self._limit},
        )
        return normalize(dump, source_kind=self.kind)


class ChannelVideosSource(_ListingSource):
    """A channel's uploads, newest first."""

    kind = "channel.videos"
    target_type = TargetType.CHANNEL

    def _extraction_target(self, target: str) -> str:
        path = target if target.startswith("@") else f"channel/{target}"
        return f"https://www.youtube.com/{path}/videos"


class SearchVideosSource(_ListingSource):
    """YouTube search results.

    Trending is deliberately absent: YouTube retired that feed, and
    `/feed/trending` now redirects to the home page.
    """

    kind = "search.videos"
    target_type = TargetType.QUERY

    def _extraction_target(self, target: str) -> str:
        return f"ytsearch{self._limit}:{target}"


class PlaylistItemsSource(_ListingSource):
    """A playlist's contents, in playlist order."""

    kind = "playlist.items"
    target_type = TargetType.PLAYLIST

    def _extraction_target(self, target: str) -> str:
        return f"https://www.youtube.com/playlist?list={target}"
