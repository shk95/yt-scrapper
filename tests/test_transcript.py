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


def test_the_videos_own_manual_captions_are_taken(recorded_dump: dict[str, Any]) -> None:
    """This video is English and its uploader wrote English captions."""
    assert recorded_dump["language"] == "en"

    track = select_caption_track(recorded_dump)

    assert track.language == "en"
    assert track.is_automatic is False


def test_the_videos_own_asr_is_taken_when_nobody_wrote_captions(
    recorded_dump: dict[str, Any],
) -> None:
    dump = dict(recorded_dump, subtitles={})

    track = select_caption_track(dump)

    assert track.language == "en"
    assert track.is_automatic is True
    assert track.name == "English (Original)"


def test_a_track_in_another_language_is_never_taken(
    recorded_dump: dict[str, Any],
) -> None:
    """An English video has 157 automatic languages and five manual ones.

    None of them is this video's language, so none of them is an answer: the
    Japanese subtitles are a translation someone uploaded and the Korean
    automatic track is a translation of a transcription. Under this policy the
    transcript is the video's own words or it is nothing.
    """
    assert "ja" in recorded_dump["subtitles"]
    assert "ko" in recorded_dump["automatic_captions"]

    candidates = caption_track_candidates(recorded_dump)

    assert {track.language for track in candidates} == {"en"}


def test_a_translated_track_is_never_a_candidate() -> None:
    """`tlang=` marks the rationed path, and this policy has no reason to use it.

    Fixture URLs are redacted, so the discriminating URL has to be built here.
    """
    dump = {
        "language": "ko",
        "subtitles": {},
        "automatic_captions": {
            "ko": [{"ext": "json3", "url": "https://example.invalid/a?lang=ko", "name": "Korean"}],
            "en": [
                {
                    "ext": "json3",
                    "url": "https://example.invalid/b?lang=ko&tlang=en",
                    "name": "English",
                }
            ],
        },
    }

    candidates = caption_track_candidates(dump)

    assert candidates
    assert all("tlang=" not in track.url for track in candidates)


def test_a_regional_track_counts_as_the_videos_language() -> None:
    """yt-dlp reports `pt` while YouTube lists the track as `pt-BR`."""
    dump = {
        "language": "pt",
        "subtitles": {
            "pt-BR": [{"ext": "json3", "url": "https://example.invalid/pt", "name": "Portuguese"}]
        },
        "automatic_captions": {},
    }

    track = select_caption_track(dump)

    assert track.language == "pt-BR"
    assert track.is_automatic is False


def test_the_orig_marker_stands_in_when_yt_dlp_reports_no_language(
    recorded_dump: dict[str, Any],
) -> None:
    """`en-orig` says which language the transcription ran in, which is the
    same question `language` answers — so either one settles it."""
    dump = dict(recorded_dump, language=None, subtitles={})

    track = select_caption_track(dump)

    assert track.language == "en"
    assert track.is_automatic is True


def test_a_video_with_no_language_at_all_falls_back_to_the_configured_order() -> None:
    """Old uploads report no language and carry no automatic captions at all.

    jNQXAC9IVRw (2005) is the real case: `language` is None, there is no `-orig`
    marker, no ASR track, and two manual languages with nothing to choose
    between them. Refusing would throw away a perfectly good transcript, so the
    configured preference decides — and only here, where the policy genuinely
    has no answer.
    """
    dump = {
        "language": None,
        "subtitles": {
            "de": [{"ext": "json3", "url": "https://example.invalid/de", "name": "German"}],
            "en": [{"ext": "json3", "url": "https://example.invalid/en", "name": "English"}],
        },
        "automatic_captions": {},
    }

    track = select_caption_track(dump, fallback_languages=("ko", "en"))

    assert track.language == "en"


def test_a_video_with_no_track_in_its_own_language_is_reported_as_not_found(
    recorded_dump: dict[str, Any],
) -> None:
    # Tracks exist — 157 automatic languages and five manual ones — just none
    # in the language the video is in. `zz` is chosen because YouTube offers a
    # translation into nearly everything, and a language it does offer would
    # pass here only because fixture URLs are redacted and so carry no `tlang`.
    dump = dict(recorded_dump, language="zz")
    assert "zz" not in dump["automatic_captions"]

    with pytest.raises(NotFoundError, match="the video's own language: zz"):
        select_caption_track(dump)


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


def test_a_refused_first_choice_falls_back_to_the_next_candidate() -> None:
    """The manual track and the transcription are both the video's own words.

    So a refusal on the better one is worth stepping past rather than failing:
    the caller gets English from the ASR instead of English from the uploader,
    which `is_automatic` reports honestly.
    """
    dump = {
        "language": "en",
        "subtitles": {
            "en": [{"ext": "json3", "url": "https://example.invalid/manual", "name": "English"}]
        },
        "automatic_captions": {
            "en-orig": [
                {
                    "ext": "json3",
                    "url": "https://example.invalid/asr",
                    "name": "English (Original)",
                }
            ]
        },
    }
    attempted: list[str] = []

    class RefusingManualEgress(FakeEgress):
        def fetch(self, url: str, *, headers: object = None) -> bytes:
            attempted.append(url)
            if url.endswith("manual"):
                raise RateLimitedError(f"{url} answered 429 on egress fake")
            return CAPTION_BODY

    transcript = TranscriptSource().collect(
        "dQw4w9WgXcQ", RefusingManualEgress(), StubRuntime(dump)
    )

    assert attempted[0].endswith("manual"), "the written captions were not tried first"
    assert transcript.language == "en"
    assert transcript.is_automatic is True
    assert transcript.segments


def test_the_last_failure_is_raised_when_no_candidate_can_be_fetched(
    recorded_dump: dict[str, Any],
) -> None:
    class RefusingEgress(FakeEgress):
        def fetch(self, url: str, *, headers: object = None) -> bytes:
            raise RateLimitedError("answered 429 on egress fake")

    with pytest.raises(RateLimitedError):
        TranscriptSource().collect("dQw4w9WgXcQ", RefusingEgress(), StubRuntime(recorded_dump))


def test_a_video_with_no_caption_tracks_at_all_says_so(recorded_dump: dict[str, Any]) -> None:
    """Distinguishing "none exist" from "none we can use" is the whole message.

    Five of forty videos in one channel sweep failed this way, and the message
    named the configured fallback languages — which reads as a policy problem
    when the truth is that the uploader turned captions off. Those are acted on
    differently: one is a setting to revisit, the other is nothing to do.
    """
    dump = dict(recorded_dump, language=None, subtitles={}, automatic_captions={})

    with pytest.raises(NotFoundError, match="no caption tracks at all"):
        select_caption_track(dump)


def test_live_chat_replay_is_not_mistaken_for_a_caption_track(
    recorded_dump: dict[str, Any],
) -> None:
    """yt-dlp files live chat replay under `subtitles`, keyed `live_chat`."""
    dump = dict(
        recorded_dump,
        language=None,
        subtitles={"live_chat": [{"ext": "json", "url": "https://example.invalid/chat"}]},
        automatic_captions={},
    )

    with pytest.raises(NotFoundError, match="no caption tracks at all"):
        select_caption_track(dump)
