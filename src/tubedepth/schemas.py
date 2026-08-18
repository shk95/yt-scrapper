"""The public contract.

Deliberately not yt-dlp's key set. That set changes when yt-dlp changes, it
carries fields that expire within hours, and several of its names describe an
implementation rather than what a viewer sees.
"""

from __future__ import annotations

from datetime import date, datetime

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


class CaptionTrackReference(BaseModel):
    """That a track exists, not how to fetch it.

    The URL is deliberately absent. Caption URLs are signed and short-lived, so
    a stored one is a guaranteed 403 later; the transcript source resolves a
    fresh one inside the same job that uses it.
    """

    language: str
    name: str | None = None
    is_automatic: bool


class VideoMetadata(BaseModel):
    video_id: str
    title: str
    description: str = ""
    channel: str | None = None
    channel_id: str | None = None
    duration_seconds: int | None = None
    categories: list[str] = []
    view_count: int | None = None
    like_count: int | None = None
    # The video's own comment count. CommentHarvest deliberately does not carry
    # one, because yt-dlp overwrites it when harvesting; this is the real number.
    comment_count: int | None = None
    caption_tracks: list[CaptionTrackReference] = []
    # snippet.tags is returned by the official Data API only to the video's
    # owner, so for everyone else this field exists nowhere else.
    tags: list[str] = []
    # The exact upload instant, always offset-aware — the part the official
    # Data API cannot give. Genuinely sometimes absent: YouTube stopped
    # returning it for at least some videos on 2026-08-18, which is why the
    # coarse date below is kept rather than derived and discarded.
    published_at: datetime | None = None
    published_date: date | None = None
    chapters: list[Chapter] = []
    most_replayed: list[ReplaySegment] = []


class TranscriptSegment(BaseModel):
    start_seconds: float
    duration_seconds: float
    text: str


class Transcript(BaseModel):
    language: str
    name: str | None = None
    is_automatic: bool = False
    segments: list[TranscriptSegment] = []
    # Built once here because it is the shape most callers actually want, and
    # because everyone joining the segments themselves joins them differently.
    full_text: str = ""


class Comment(BaseModel):
    comment_id: str
    # None, not the string "root". A sentinel that looks like an identifier is
    # a bug waiting for someone to compare against it.
    parent_id: str | None = None
    text: str
    like_count: int | None = None
    author: str | None = None
    author_id: str | None = None
    author_is_uploader: bool = False
    author_is_verified: bool = False
    is_pinned: bool = False
    # yt-dlp calls this `is_favorited`. What YouTube shows is a heart from the
    # channel, and "favorited" reads like something the viewer did.
    is_hearted_by_uploader: bool = False
    published_at: datetime | None = None
    published_text: str | None = None


class CommentHarvest(BaseModel):
    sort: str
    retrieved_count: int = 0
    # There is deliberately no "reported_total". With getcomments on, yt-dlp
    # overwrites comment_count with the number it retrieved, so carrying it
    # would present our own count as YouTube's. The video's real count is on
    # VideoMetadata, where nothing overwrites it.
    # Whether the harvest ran out of comments or ran into its limit. The
    # difference is the difference between data and a misleading number, and
    # only the harvester knows which happened.
    is_truncated: bool = False
    # Flat, threaded by parent_id, rather than nested. A fifty-thousand-comment
    # nested document is pathological to parse and to diff, and any caller that
    # wants a tree builds one in five lines.
    comments: list[Comment] = []


class ListedVideo(BaseModel):
    """A video as it appears in a listing.

    Deliberately thinner than VideoMetadata: a flat listing carries only what
    YouTube puts in the grid. Duration and view count are the two fields worth
    having here anyway, because they are what anyone filters on before
    spending a request per video to collect the rest.
    """

    video_id: str
    title: str | None = None
    duration_seconds: int | None = None
    view_count: int | None = None
    channel: str | None = None
    channel_id: str | None = None
    published_at: datetime | None = None


class VideoListing(BaseModel):
    source_kind: str
    listing_id: str | None = None
    title: str | None = None
    videos: list[ListedVideo] = []
    # Deleted and private entries, which yt-dlp leaves as placeholders. Counted
    # rather than dropped silently: "twelve of fourteen" is a different fact
    # from "twelve", and only this layer can tell the difference.
    skipped_count: int = 0
