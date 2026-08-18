"""Making a recorded dump safe to commit.

A raw yt-dlp dump cannot go into git. It carries roughly thirty-seven signed
googlevideo URLs and one signed caption URL per language — every one of them
expiring within hours, bloating the diff, and reading as a credential to
gitleaks. This module is the reason `tubedepth capture-fixture` exists rather
than `yt-dlp --dump-json > fixture.json`.

The two rules differ on purpose. Format URLs are dropped outright: nothing here
reads them and they are most of the bytes. Caption URLs are *replaced*, because
the transcript source selects a track by shape and deleting the key would make
that selection impossible to test without the network.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

REDACTED_CAPTION_URL = "https://redacted.invalid/timedtext"
REDACTED_AVATAR_URL = "https://redacted.invalid/avatar"

# Fields on a comment that identify a person rather than describe a comment.
AUTHOR_IDENTITY_KEYS = ("author", "author_id", "author_url", "author_thumbnail")

# Whole keys that carry signed URLs and that nothing in this project reads.
DROPPED_KEYS = ("formats", "requested_formats", "url", "thumbnail")

CAPTION_BUCKETS = ("subtitles", "automatic_captions")


def redact_for_fixture(dump: Mapping[str, Any]) -> dict[str, Any]:
    """Return a committable copy of `dump`. The original is untouched."""
    redacted = copy.deepcopy(dict(dump))

    for key in DROPPED_KEYS:
        redacted.pop(key, None)

    for bucket in CAPTION_BUCKETS:
        for tracks in (redacted.get(bucket) or {}).values():
            for track in tracks:
                if "url" in track:
                    track["url"] = REDACTED_CAPTION_URL

    for thumbnail in redacted.get("thumbnails") or []:
        thumbnail.pop("url", None)

    _anonymize_comment_authors(redacted)

    return redacted


def _anonymize_comment_authors(redacted: dict[str, Any]) -> None:
    """Replace who wrote a comment while keeping that someone did.

    Display names, channel ids, profile URLs and avatars are personal data
    belonging to people who did not agree to appear in this repository, and git
    keeps them after the file is deleted. Nothing under test needs the
    identity — only that comments by one person share an author, which is what
    threading and "did the uploader reply" are checked against. So the mapping
    is stable within a dump rather than random per comment.
    """
    pseudonyms: dict[str, int] = {}

    for comment in redacted.get("comments") or []:
        if not any(key in comment for key in AUTHOR_IDENTITY_KEYS):
            continue
        real = str(comment.get("author_id") or comment.get("author") or "")
        index = pseudonyms.setdefault(real, len(pseudonyms) + 1)

        if "author" in comment:
            comment["author"] = f"@author{index}"
        if "author_id" in comment:
            comment["author_id"] = f"UC_redacted_{index:04d}"
        if "author_url" in comment:
            comment["author_url"] = f"https://redacted.invalid/@author{index}"
        if "author_thumbnail" in comment:
            comment["author_thumbnail"] = REDACTED_AVATAR_URL
