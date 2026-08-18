"""Choosing which caption track to fetch, and reading what comes back.

Selection is where the interesting decisions live. A video can carry a manual
track and an automatic one for the same language, five manual languages and a
hundred and fifty-seven automatic ones, and the caller usually just says "en".
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tubedepth.egress.transport import DirectEgress, Egress
from tubedepth.errors import NotFoundError, RateLimitedError
from tubedepth.sources.transcript import (
    TranscriptSource,
    caption_track_candidates,
    parse_json3,
    select_caption_track,
)


class FakeEgress(DirectEgress):
    """A real Egress with its one network call replaced by the subclass."""


CAPTION_BODY = json.dumps(
    {"events": [{"tStartMs": 0, "dDurationMs": 900, "segs": [{"utf8": "hi"}]}]}
).encode()


class StubRuntime:
    """A YtdlpRuntime that answers from a recorded dump instead of a network."""

    def __init__(self, dump: Mapping[str, Any]) -> None:
        self._dump = dump

    def extract(
        self,
        target: str,
        *,
        egress: Egress,
        options: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        return self._dump


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
    track = select_caption_track(recorded_dump, languages=("ja",))

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

    track = select_caption_track(recorded_dump, languages=("en",))

    assert track.is_automatic is False


def test_an_automatic_track_is_used_when_no_manual_one_exists(
    recorded_dump: dict[str, Any],
) -> None:
    assert "ko" not in recorded_dump["subtitles"]
    assert "ko" in recorded_dump["automatic_captions"]

    track = select_caption_track(recorded_dump, languages=("ko",))

    assert track.is_automatic is True
    assert track.language == "ko"


def test_a_language_the_video_does_not_have_is_reported_as_not_found(
    recorded_dump: dict[str, Any],
) -> None:
    with pytest.raises(NotFoundError, match="no caption track"):
        select_caption_track(recorded_dump, languages=("zz",))


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


def test_korean_is_taken_ahead_of_a_manual_english_track(
    recorded_dump: dict[str, Any],
) -> None:
    """Language order decides first, and Korean is first.

    This video has English captions a person wrote and no Korean ones, so the
    Korean track that wins here is a machine translation of a machine
    transcription. That is the accepted cost of ranking language above
    provenance: a caller who asked for Korean gets Korean whenever there is
    any Korean to get.
    """
    assert "en" in recorded_dump["subtitles"]
    assert "ko" not in recorded_dump["subtitles"]
    assert "ko" in recorded_dump["automatic_captions"]

    track = select_caption_track(recorded_dump, languages=("ko", "en"))

    assert track.language == "ko"
    assert track.is_automatic is True


def test_a_manual_korean_track_beats_an_automatic_one(
    recorded_dump: dict[str, Any],
) -> None:
    """Provenance still decides *within* a language."""
    written_by_hand = {"ext": "json3", "url": "https://example.invalid/ko", "name": "Korean"}
    dump = dict(recorded_dump, subtitles={"ko": [written_by_hand]})

    track = select_caption_track(dump, languages=("ko", "en"))

    assert track.language == "ko"
    assert track.is_automatic is False


def test_english_is_used_only_when_the_video_has_no_korean_track_at_all(
    recorded_dump: dict[str, Any],
) -> None:
    english_only = {
        "subtitles": recorded_dump["subtitles"],
        "automatic_captions": {
            key: value
            for key, value in recorded_dump["automatic_captions"].items()
            if not key.startswith("ko")
        },
    }

    track = select_caption_track(english_only, languages=("ko", "en"))

    assert track.language == "en"
    assert track.is_automatic is False


def test_the_original_transcription_beats_a_translation_into_the_same_language(
    recorded_dump: dict[str, Any],
) -> None:
    """`-orig` is yt-dlp's marker for the language the ASR actually ran in.

    The plain `en` key on this video is that transcription round-tripped
    through the translator; `en-orig` is the transcription itself.
    """
    stripped = dict(recorded_dump, subtitles={})

    track = select_caption_track(stripped, languages=("en",))

    assert track.language == "en"
    assert track.name == "English (Original)"


def test_a_translation_is_used_when_the_original_language_was_not_asked_for(
    recorded_dump: dict[str, Any],
) -> None:
    stripped = dict(recorded_dump)
    stripped["subtitles"] = {}

    track = select_caption_track(stripped, languages=("ko",))

    assert track.language == "ko"
    assert track.is_automatic is True


def test_no_track_in_any_preferred_language_names_all_of_them(
    recorded_dump: dict[str, Any],
) -> None:
    with pytest.raises(NotFoundError) as caught:
        select_caption_track({"subtitles": {}, "automatic_captions": {}}, languages=("ko", "en"))

    assert "ko" in str(caught.value)
    assert "en" in str(caught.value)


def test_the_ranked_candidates_put_every_korean_track_ahead_of_english(
    recorded_dump: dict[str, Any],
) -> None:
    candidates = caption_track_candidates(recorded_dump, languages=("ko", "en"))

    ranked = [(track.language, track.is_automatic) for track in candidates]
    assert ranked[0] == ("ko", True)
    assert ("en", False) in ranked
    assert ranked.index(("en", False)) > 0


def test_a_refused_first_choice_falls_back_to_the_next_candidate() -> None:
    """YouTube throttles `tlang=` far harder than it throttles the track itself.

    Measured back to back on one address: the Korean translation of
    dQw4w9WgXcQ's captions answered 429 four times out of four while the
    English track it is translated from answered 200 four times out of four.
    Korean ranks first, so on an English video the preferred candidate is the
    fragile one, and stopping there would read as "transcripts are broken"
    rather than "this one track is rationed".

    Fixture URLs are redacted, so this builds its own dump — the two tracks
    have to be told apart by URL for the test to mean anything.
    """
    dump = {
        "subtitles": {
            "en": [{"ext": "json3", "url": "https://example.invalid/en", "name": "English"}]
        },
        "automatic_captions": {
            "ko": [{"ext": "json3", "url": "https://example.invalid/ko?tlang=ko", "name": "Korean"}]
        },
    }
    attempted: list[str] = []

    class ThrottledEgress(FakeEgress):
        def fetch(self, url: str, *, headers: object = None) -> bytes:
            attempted.append(url)
            if "tlang=" in url:
                raise RateLimitedError(f"{url} answered 429 on egress fake")
            return CAPTION_BODY

    transcript = TranscriptSource(languages=("ko", "en")).collect(
        "dQw4w9WgXcQ", ThrottledEgress(), StubRuntime(dump)
    )

    assert attempted[0].endswith("tlang=ko"), "the Korean translation was not tried first"
    assert transcript.language == "en"
    assert transcript.is_automatic is False
    assert transcript.segments


def test_the_last_failure_is_raised_when_no_candidate_can_be_fetched(
    recorded_dump: dict[str, Any],
) -> None:
    class RefusingEgress(FakeEgress):
        def fetch(self, url: str, *, headers: object = None) -> bytes:
            raise RateLimitedError("answered 429 on egress fake")

    source = TranscriptSource(languages=("ko", "en"))

    with pytest.raises(RateLimitedError):
        source.collect("dQw4w9WgXcQ", RefusingEgress(), StubRuntime(recorded_dump))
