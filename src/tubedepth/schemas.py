"""The public contract.

Deliberately not yt-dlp's key set. That set changes when yt-dlp changes, it
carries fields that expire within hours, and several of its names describe an
implementation rather than what a viewer sees.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Chapter(BaseModel):
    title: str
    start_seconds: float
    end_seconds: float | None = None


class ReplaySegment(BaseModel):
    """One bucket of YouTube's "most replayed" graph.

    `rank` is derived here rather than left to callers: the graph arrives in
    playback order, and the ranking is the thing anyone actually wants.
    """

    rank: int
    start_seconds: float
    end_seconds: float
    score: float = Field(ge=0.0, le=1.0)


class VideoMetadata(BaseModel):
    video_id: str
    title: str
    channel_id: str | None = None
    # snippet.tags is returned by the official Data API only to the video's
    # owner, so for everyone else this field exists nowhere else.
    tags: list[str] = []
    # The exact upload instant, always offset-aware. yt-dlp also carries a
    # coarse upload_date; the instant is the part worth having.
    published_at: datetime | None = None
    chapters: list[Chapter] = []
    most_replayed: list[ReplaySegment] = []
