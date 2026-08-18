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

    return redacted
