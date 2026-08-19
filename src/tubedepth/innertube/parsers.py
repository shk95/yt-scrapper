"""Turning InnerTube responses into the public contract.

Each parser declares the renderers it accepts and, where an empty answer is
legitimate, the marker that proves the response really is empty. Nothing here
reads a fixed path.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from typing import Any

from ..errors import ExtractionError
from ..schemas import (
    ChannelAbout,
    CommunityPost,
    CommunityPosts,
    RelatedVideo,
    RelatedVideos,
)
from .renderers import collect, find_all, flatten_text, observed_renderers

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


def _link_text(link: Mapping[str, Any]) -> str | None:
    """The visible URL, not the redirect wrapper.

    YouTube wraps every external link in `youtube.com/redirect?...&redir_token=`
    with a token that is per-response and expires. Storing that gives an
    artifact full of dead credentials-shaped strings; the displayed text is the
    actual address and is stable.
    """
    content = link.get("link")
    text = flatten_text(content) if content is not None else None
    return text or None


def parse_channel_about(
    payload: Mapping[str, Any],
    *,
    channel_id: str,
    metadata: Mapping[str, Any] | None = None,
) -> ChannelAbout:
    """The channel's own about panel, and nothing that merely resembles it.

    This reads `aboutChannelViewModel` and refuses anything else. That is not
    defensiveness for its own sake: the first version of this source sent no
    `params`, received the channel *home* tab, and — searching by renderer name
    the way everything here does — picked up the featured video's description
    and returned it as the channel's. Join date, country and links came back
    empty, which is the entire reason this source exists rather than using
    yt-dlp. The result looked like a successful collection.

    So the panel is required. A response without it is an `ExtractionError`
    naming what was missing, because "we fetched the wrong page" and "this
    channel has no about text" must never look the same.
    """
    about = next(find_all(payload, "aboutChannelViewModel"), None)
    if about is None:
        observed = ", ".join(sorted(observed_renderers(payload))[:12])
        raise ExtractionError(
            "no aboutChannelViewModel in the response — this is not the about panel; "
            f"observed instead: {observed}"
        )

    # The channel's own metadata, when the caller has the response it lives in.
    # Optional rather than required because the about panel is what this source
    # is for; the name and tags are an enrichment that happens to be free.
    channel_metadata = next(find_all(metadata or {}, "channelMetadataRenderer"), {})
    keywords = channel_metadata.get("keywords")

    subscriber_text = flatten_text(about.get("subscriberCountText"))
    return ChannelAbout(
        channel_id=about.get("channelId") or channel_id,
        description=flatten_text(about.get("description")) or None,
        subscriber_count_approximate=_rounded_count(subscriber_text),
        subscriber_count_text=subscriber_text,
        country=flatten_text(about.get("country")) or None,
        joined_text=flatten_text(about.get("joinedDateText")) or None,
        view_count=_exact_count(flatten_text(about.get("viewCountText"))),
        video_count=_exact_count(flatten_text(about.get("videoCountText"))),
        handle=about.get("displayCanonicalChannelUrl") or None,
        name=flatten_text(channel_metadata.get("title")) or None,
        tags=_split_keywords(keywords) if isinstance(keywords, str) else [],
        avatar_url=_largest_thumbnail(channel_metadata.get("avatar")),
        links=[
            text
            for link in find_all(about, "channelExternalLinkViewModel")
            if (text := _link_text(link))
        ],
    )


def _split_keywords(keywords: str) -> list[str]:
    """`'Official "rick astley" rickroll'` to three tags.

    YouTube ships channel keywords as one space-separated string with quotes
    around the multi-word ones, which is a shell-argument convention rather
    than a list. Splitting on whitespace alone shreds every phrase tag.
    """
    return [tag for tag in shlex.split(keywords) if tag]


def _largest_thumbnail(avatar: Mapping[str, Any] | None) -> str | None:
    sources = (avatar or {}).get("thumbnails") or []
    best = max(sources, key=lambda source: source.get("width", 0), default=None)
    return best.get("url") if best else None


def _exact_count(text: str | None) -> int | None:
    """`"2,536,701,615 views"` to an integer.

    Exact rather than rounded, unlike the subscriber count — YouTube publishes
    the real figure here, and it is one of the things the official Data API
    does not give for a channel at all.
    """
    if not text:
        return None
    digits = "".join(character for character in text if character.isdigit())
    return int(digits) if digits else None
