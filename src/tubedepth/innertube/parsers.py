"""Turning InnerTube responses into the public contract.

Each parser declares the renderers it accepts and, where an empty answer is
legitimate, the marker that proves the response really is empty. Nothing here
reads a fixed path.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..schemas import (
    ChannelAbout,
    CommunityPost,
    CommunityPosts,
    RelatedVideo,
    RelatedVideos,
)
from .renderers import collect, find_all, flatten_text

# The old name stays first-class so a rollback does not break us in the other
# direction: compactVideoRenderer was the shape until YouTube moved to
# lockupViewModel, and recordings of both should keep parsing.
RELATED_RENDERERS = ("lockupViewModel", "compactVideoRenderer", "videoRenderer")
COMMUNITY_RENDERERS = ("backstagePostRenderer", "postRenderer")
# A channel with no community posts says so with a message renderer. That
# marker is the whole difference between "nothing here" and "unreadable".
EMPTY_MARKERS = ("messageRenderer", "emptyStateRenderer")

VIDEO_IDENTIFIER = re.compile(r"\A[A-Za-z0-9_-]{11}\Z")
ROUNDED_COUNT = re.compile(r"([\d.,]+)\s*([KMB])?", re.IGNORECASE)
MULTIPLIERS = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _video_identifier(entry: Mapping[str, Any]) -> str | None:
    """Find the video id in whichever field this renderer keeps it."""
    for key in ("videoId", "contentId", "entityId"):
        value = entry.get(key)
        if isinstance(value, str) and VIDEO_IDENTIFIER.match(value):
            return value
    for candidate in find_all(entry, "watchEndpoint"):
        value = candidate.get("videoId")
        if isinstance(value, str) and VIDEO_IDENTIFIER.match(value):
            return value
    return None


def _first_text(entry: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        text = flatten_text(entry.get(key))
        if text:
            return text
    return None


def parse_related_videos(payload: Mapping[str, Any], *, video_id: str) -> RelatedVideos:
    matched = next(
        (name for name in RELATED_RENDERERS if next(find_all(payload, name), None) is not None),
        None,
    )
    entries = collect(payload, accepted=RELATED_RENDERERS, contract="related videos")

    items: list[RelatedVideo] = []
    for entry in entries:
        identifier = _video_identifier(entry)
        if identifier is None:
            continue
        metadata = next(find_all(entry, "lockupMetadataViewModel"), entry)
        items.append(
            RelatedVideo(
                video_id=identifier,
                title=_first_text(metadata, "title", "headline")
                or flatten_text(next(find_all(entry, "title"), None)),
                channel=_first_text(metadata, "shortBylineText", "longBylineText"),
                view_count_text=_first_text(entry, "viewCountText", "shortViewCountText"),
                duration_text=_first_text(entry, "lengthText"),
            )
        )

    return RelatedVideos(video_id=video_id, items=items, renderer_shape=matched)


def parse_community_posts(payload: Mapping[str, Any], *, channel_id: str) -> CommunityPosts:
    entries = collect(
        payload,
        accepted=COMMUNITY_RENDERERS,
        contract="community posts",
        empty_markers=EMPTY_MARKERS,
    )
    return CommunityPosts(
        channel_id=channel_id,
        posts=[
            CommunityPost(
                post_id=entry.get("postId"),
                text=flatten_text(entry.get("contentText")),
                published_text=flatten_text(entry.get("publishedTimeText")),
                vote_count_text=flatten_text(entry.get("voteCount")),
            )
            for entry in entries
        ],
    )


def _rounded_count(text: str | None) -> int | None:
    """Parse "4.53M subscribers" into 4,530,000 — and no more precisely than that."""
    if not text:
        return None
    match = ROUNDED_COUNT.search(text.replace(",", ""))
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return int(value * MULTIPLIERS.get((match.group(2) or "").lower(), 1))


def parse_channel_about(payload: Mapping[str, Any], *, channel_id: str) -> ChannelAbout:
    subscriber_text = next(
        (
            text
            for candidate in find_all(payload, "metadataParts")
            if (text := flatten_text(candidate.get("text"))) and "subscriber" in text.lower()
        ),
        None,
    )
    if subscriber_text is None:
        subscriber_text = next(
            (
                text
                for key in ("subscriberCountText", "videoCountText")
                for candidate in find_all(payload, key)
                if (text := flatten_text(candidate)) and "subscriber" in text.lower()
            ),
            None,
        )

    header = next(find_all(payload, "pageHeaderViewModel"), {})
    return ChannelAbout(
        channel_id=channel_id,
        name=flatten_text(header.get("title")) if header else None,
        description=flatten_text(next(find_all(payload, "description"), None)),
        subscriber_count_approximate=_rounded_count(subscriber_text),
        subscriber_count_text=subscriber_text,
        country=flatten_text(next(find_all(payload, "country"), None)),
        joined_text=flatten_text(next(find_all(payload, "joinedDateText"), None)),
    )
