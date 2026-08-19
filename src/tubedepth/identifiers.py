"""Turning whatever a caller pasted into the identifier YouTube uses.

Callers paste watch URLs, share links, bare identifiers and channel handles
interchangeably, and every one of those has to reach the same cache entry.
Normalizing at the edge is what makes the cache key trustworthy: two spellings
of the same video that produced two artifacts would double the work and halve
the hit rate without anything looking wrong.
"""

from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import parse_qs, urlparse

from .errors import ValidationError


class TargetType(StrEnum):
    """What a source collects about.

    Which normalizer applies is a property of the source rather than of the
    caller: a channel handle run through the video normalizer is rejected as
    malformed, and a video id run through the channel one is accepted and then
    fails minutes later inside the extractor.
    """

    VIDEO = "video"
    CHANNEL = "channel"
    PLAYLIST = "playlist"
    QUERY = "query"
    REGION = "region"


# YouTube video ids are eleven characters of URL-safe base64.
VIDEO_IDENTIFIER = re.compile(r"\A[A-Za-z0-9_-]{11}\Z")
# Channel ids are always UC plus twenty-two more.
CHANNEL_IDENTIFIER = re.compile(r"\AUC[A-Za-z0-9_-]{22}\Z")
CHANNEL_HANDLE = re.compile(r"\A@[A-Za-z0-9_.-]{3,30}\Z")
# Playlist ids vary in length and prefix far more than the others do.
PLAYLIST_IDENTIFIER = re.compile(r"\A[A-Za-z0-9_-]{12,64}\Z")


def normalize_video_identifier(value: str) -> str:
    """Return the eleven-character video id behind `value`."""
    parsed = urlparse(value)
    if not parsed.scheme:
        candidate = value
    elif parsed.netloc.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/")
    elif parsed.path.startswith(("/shorts/", "/embed/", "/live/", "/v/")):
        candidate = parsed.path.split("/")[2]
    else:
        # .get, not [], so a URL with no v parameter falls through to the
        # validation below and leaves as a domain error rather than a KeyError.
        candidate = parse_qs(parsed.query).get("v", [""])[0]
    if not VIDEO_IDENTIFIER.match(candidate):
        raise ValidationError(f"video identifier is not valid: {value}")
    return candidate


def normalize_channel_identifier(value: str) -> str:
    """Return the handle or channel id behind `value`."""
    parsed = urlparse(value)
    if parsed.scheme:
        segments = [segment for segment in parsed.path.split("/") if segment]
        if segments and segments[0] == "channel" and len(segments) > 1:
            candidate = segments[1]
        elif segments:
            candidate = segments[0]
        else:
            candidate = ""
    else:
        candidate = value

    if CHANNEL_IDENTIFIER.match(candidate) or CHANNEL_HANDLE.match(candidate):
        return candidate
    raise ValidationError(f"channel identifier is not valid: {value}")


def normalize_playlist_identifier(value: str) -> str:
    """Return the playlist id behind `value`.

    A link handed out from inside a video carries both a video id and a list
    id; the list is the one being asked for.
    """
    parsed = urlparse(value)
    candidate = parse_qs(parsed.query).get("list", [""])[0] if parsed.scheme else value

    if PLAYLIST_IDENTIFIER.match(candidate):
        return candidate
    raise ValidationError(f"playlist identifier is not valid: {value}")


def normalize_search_query(value: str) -> str:
    """Trim a query and refuse an empty one.

    The one target type with no canonical form — normalizing further would
    change what was searched for.
    """
    query = value.strip()
    if not query:
        raise ValidationError("search query is empty")
    return query


def normalize_region_code(value: str) -> str:
    """An ISO 3166-1 alpha-2 region, upper-cased.

    The plausible mistake is alpha-3, because ISO 3166 has both and `KOR` looks
    more like a country than `KR` does. This endpoint takes only alpha-2 and
    answers 400 for the other, so refusing here costs nothing while refusing
    after the request spends a quota unit to be told the same thing.
    """
    code = value.strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise ValidationError(f"not an ISO 3166-1 alpha-2 region code: {value}")
    return code


_NORMALIZERS = {
    TargetType.VIDEO: normalize_video_identifier,
    TargetType.CHANNEL: normalize_channel_identifier,
    TargetType.PLAYLIST: normalize_playlist_identifier,
    TargetType.QUERY: normalize_search_query,
    TargetType.REGION: normalize_region_code,
}


def normalize_target(target_type: TargetType, value: str) -> str:
    return _NORMALIZERS[target_type](value)
