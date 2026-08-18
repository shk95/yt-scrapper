"""Tests that actually reach YouTube.

Deselected by default and never run in CI: a GitHub runner is a datacenter
address and YouTube bot-checks those, so a red build here would say nothing
about the change under test. Run deliberately with `just contract`, on a
residential connection.

What CI proves is that our parsers did not regress against a recorded
response. What this proves is that the recording still resembles what YouTube
is currently sending. Those are different claims and only one of them is
checked automatically.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tubedepth.collection import CollectionService
from tubedepth.payload_store import PayloadStore
from tubedepth.sources.ytdlp_runtime import LibraryYtdlpRuntime

pytestmark = pytest.mark.live


def test_live_a_real_extraction_still_yields_tags_and_a_hundred_heatmap_buckets(
    tmp_path: Path,
) -> None:
    payloads = PayloadStore(tmp_path)
    service = CollectionService(runtime=LibraryYtdlpRuntime(), payloads=payloads)

    collected = service.collect("video.metadata", "dQw4w9WgXcQ")

    payload = json.loads(payloads.read(collected.payload.digest))
    assert payload["video_id"] == "dQw4w9WgXcQ"
    assert payload["tags"], "tags disappeared — the Data API withholds these, so we are the source"
    assert len(payload["most_replayed"]) == 100
    assert payload["published_at"].startswith("2009-10-25")


def test_live_a_real_transcript_still_parses_into_timed_segments(tmp_path: Path) -> None:
    payloads = PayloadStore(tmp_path)
    service = CollectionService(runtime=LibraryYtdlpRuntime(), payloads=payloads)

    collected = service.collect("video.transcript", "dQw4w9WgXcQ")

    payload = json.loads(payloads.read(collected.payload.digest))
    assert payload["segments"], "caption body parsed to nothing — json3 shape may have changed"
    assert payload["full_text"].strip()
    assert payload["segments"][0]["duration_seconds"] > 0
