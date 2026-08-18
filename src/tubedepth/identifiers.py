"""Turning whatever a caller pasted into the identifier YouTube uses.

Callers paste watch URLs, share links, bare identifiers and channel handles
interchangeably, and every one of those has to reach the same cache entry.
Normalizing at the edge is what makes the cache key trustworthy: two spellings
of the same video that produced two artifacts would double the work and halve
the hit rate without anything looking wrong.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from .errors import ValidationError

# YouTube video ids are eleven characters of URL-safe base64.
VIDEO_IDENTIFIER = re.compile(r"\A[A-Za-z0-9_-]{11}\Z")


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
