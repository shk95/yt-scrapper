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
from tubedepth.sources import SourceRegistry
from tubedepth.sources.comments import CommentsSource
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


def test_live_a_real_comment_harvest_still_threads_replies(tmp_path: Path) -> None:
    payloads = PayloadStore(tmp_path)
    service = CollectionService(
        runtime=LibraryYtdlpRuntime(),
        payloads=payloads,
        registry=_registry_with(CommentsSource(sort="top", limit=40)),
    )

    collected = service.collect("video.comments", "dQw4w9WgXcQ")

    payload = json.loads(payloads.read(collected.payload.digest))
    identifiers = {comment["comment_id"] for comment in payload["comments"]}
    replies = [c for c in payload["comments"] if c["parent_id"] is not None]
    assert payload["comments"], "no comments came back — the extractor may have changed"
    assert replies, "no replies came back; threading is untested if this stays empty"
    assert all(reply["parent_id"] in identifiers for reply in replies)


def _registry_with(source: object) -> SourceRegistry:
    """A registry holding one source, so a live test bounds its own cost.

    The shipped CommentsSource defaults to 200 comments, which is a minute of
    wall clock and 200 rows of somebody's personal data for a test that needs
    neither.
    """
    registry = SourceRegistry()
    registry.register(source)  # type: ignore[arg-type]
    return registry
