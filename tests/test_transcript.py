"""Choosing which caption track to fetch, and reading what comes back.

Selection is where the interesting decisions live. A video can carry a manual
track and an automatic one for the same language, five manual languages and a
hundred and fifty-seven automatic ones, and the caller usually just says "en".
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from tubedepth.errors import NotFoundError
from tubedepth.sources.transcript import parse_json3, select_caption_track

FIXTURES = Path(__file__).parent / "fixtures/ytdlp/video_metadata"


@pytest.fixture
def recorded_dump() -> dict[str, Any]:
    with gzip.open(FIXTURES / "2026-08-18-dQw4w9WgXcQ.json.gz", "rt", encoding="utf-8") as handle:
        return json.load(handle)


def test_selection_returns_the_json3_track_for_the_requested_language(
    recorded_dump: dict[str, Any],
) -> None:
    # json3 specifically: it is the only format that carries per-segment
    # timings as data rather than as text that has to be re-parsed.
    track = select_caption_track(recorded_dump, language="ja")

    assert track.language == "ja"
    assert track.format == "json3"
    assert track.url


def test_a_manual_track_is_preferred_over_an_automatic_one(
    recorded_dump: dict[str, Any],
) -> None:
    # This video has both for English. The manual one is written by a person;
    # the automatic one is a transcription and will disagree with it.
    assert "en" in recorded_dump["subtitles"]
    assert "en" in recorded_dump["automatic_captions"]

    track = select_caption_track(recorded_dump, language="en")

    assert track.is_automatic is False


def test_an_automatic_track_is_used_when_no_manual_one_exists(
    recorded_dump: dict[str, Any],
) -> None:
    assert "ko" not in recorded_dump["subtitles"]
    assert "ko" in recorded_dump["automatic_captions"]

    track = select_caption_track(recorded_dump, language="ko")

    assert track.is_automatic is True
    assert track.language == "ko"


def test_a_language_the_video_does_not_have_is_reported_as_not_found(
    recorded_dump: dict[str, Any],
) -> None:
    with pytest.raises(NotFoundError, match="no caption track"):
        select_caption_track(recorded_dump, language="zz")


@pytest.fixture
def recorded_json3() -> dict[str, Any]:
    path = Path(__file__).parent / "fixtures/timedtext/2026-08-18-dQw4w9WgXcQ-en-json3.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def test_json3_events_become_segments_timed_in_seconds(
    recorded_json3: dict[str, Any],
) -> None:
    # YouTube times these in milliseconds. Seconds everywhere in the contract,
    # with the unit in the field name, so no caller has to guess or divide.
    transcript = parse_json3(recorded_json3)

    assert len(transcript.segments) == 61
    first = transcript.segments[0]
    assert first.start_seconds == pytest.approx(1.36)
    assert first.duration_seconds == pytest.approx(1.68)
    assert first.text == "[♪♪♪]"


def test_the_full_text_joins_every_segment(recorded_json3: dict[str, Any]) -> None:
    # The single most-asked-for shape: one string to search, feed to a model,
    # or diff. Building it here means every caller gets the same one.
    transcript = parse_json3(recorded_json3)

    assert "We're no strangers to love" in transcript.full_text
    assert transcript.full_text.count("\n") < len(transcript.full_text)


def test_a_segment_split_across_runs_is_joined_into_one_text() -> None:
    # YouTube splits a caption line into several `segs` when it styles part of
    # it. Treating each seg as its own segment would shred the line, and the
    # recorded fixture happens not to contain one — so this is the case that
    # would otherwise be found in production.
    payload = {
        "events": [
            {"tStartMs": 100, "dDurationMs": 900, "segs": [{"utf8": "hello "}, {"utf8": "world"}]}
        ]
    }

    transcript = parse_json3(payload)

    assert [segment.text for segment in transcript.segments] == ["hello world"]


def test_events_carrying_no_text_are_dropped(recorded_json3: dict[str, Any]) -> None:
    # json3 uses text-free events for window positioning. They are not
    # captions, and a consumer counting segments would be counting furniture.
    payload = {"events": [{"tStartMs": 0, "dDurationMs": 10, "id": 1}, *recorded_json3["events"]]}

    assert len(parse_json3(payload).segments) == 61
