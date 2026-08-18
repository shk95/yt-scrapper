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
from tubedepth.egress.transport import DirectEgress
from tubedepth.payload_store import PayloadStore
from tubedepth.sources import SourceRegistry
from tubedepth.sources.comments import CommentsSource
from tubedepth.sources.transcript import select_caption_track
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
    # The date, not the instant: YouTube stopped returning `timestamp` for at
    # least some videos on 2026-08-18, and the coarse date is what survived.
    # Asserting the instant here would make this test fail for a reason that
    # is upstream's rather than ours.
    assert payload["published_date"] == "2009-10-25"


def test_live_a_real_transcript_still_parses_into_timed_segments(tmp_path: Path) -> None:
    payloads = PayloadStore(tmp_path)
    service = CollectionService(runtime=LibraryYtdlpRuntime(), payloads=payloads)

    collected = service.collect("video.transcript", "dQw4w9WgXcQ")

    payload = json.loads(payloads.read(collected.payload.digest))
    assert payload["segments"], "caption body parsed to nothing — json3 shape may have changed"
    assert payload["full_text"].strip()
    assert payload["segments"][0]["duration_seconds"] > 0


def test_live_a_korean_video_yields_its_own_transcription_not_a_translation(
    tmp_path: Path,
) -> None:
    """The case the Korean-first ordering exists for.

    A Korean video with no manual captions must resolve to `ko` marked
    `kind=asr` with no `tlang` — the transcription itself. The translated
    variant is drawn from a small per-address budget that a sweep exhausts in
    three or four requests, so a change that quietly started fetching it would
    turn a limitless path into a rationed one, and the only visible symptom
    would be transcripts arriving in English.
    """
    dump = LibraryYtdlpRuntime().extract("9bZkp7q19f0", egress=DirectEgress())
    assert not dump.get("subtitles"), "this video gained manual captions; pick another"

    track = select_caption_track(dump, languages=("ko", "en"))

    assert track.language == "ko"
    assert "tlang=" not in track.url, "took a translation where the original exists"

    payloads = PayloadStore(tmp_path)
    service = CollectionService(runtime=LibraryYtdlpRuntime(), payloads=payloads)
    collected = service.collect("video.transcript", "9bZkp7q19f0")

    payload = json.loads(payloads.read(collected.payload.digest))
    assert payload["language"] == "ko"
    assert payload["segments"]


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
