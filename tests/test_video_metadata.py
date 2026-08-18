"""Normalizing a yt-dlp dump into the public contract.

yt-dlp's 83 keys are not the API. They change when yt-dlp changes, they carry
things that expire, and several of them are named after implementation details
rather than after what YouTube shows a viewer. Everything here runs against a
recorded dump with the network blocked.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from tubedepth.sources.video_metadata import normalize

FIXTURES = Path(__file__).parent / "fixtures/ytdlp/video_metadata"


def load(name: str) -> dict[str, Any]:
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def recorded_dump() -> dict[str, Any]:
    """A real dump from a video with no chapters."""
    return load("2026-08-18-dQw4w9WgXcQ.json.gz")


@pytest.fixture
def chaptered_dump() -> dict[str, Any]:
    """A real dump from a video that does have chapters."""
    return load("2026-08-18-nfgdJyL-Jmg-chaptered.json.gz")


def test_a_video_without_chapters_normalizes_to_an_empty_chapter_list(
    recorded_dump: dict[str, Any],
) -> None:
    # yt-dlp reports null. Absence is a fact about the video, not a hole in the
    # contract, and a nullable list makes every caller write the same guard.
    assert recorded_dump["chapters"] is None

    assert normalize(recorded_dump).chapters == []


def test_the_heatmap_becomes_a_most_replayed_list_ranked_by_descending_score(
    recorded_dump: dict[str, Any],
) -> None:
    # YouTube ships 100 equal-width buckets in playback order. The ranking is
    # what anyone actually wants — "where do people rewatch" — so deriving it
    # once here beats every caller re-deriving it, and beats every caller
    # getting it subtly different.
    assert len(recorded_dump["heatmap"]) == 100

    most_replayed = normalize(recorded_dump).most_replayed

    assert [segment.rank for segment in most_replayed[:3]] == [1, 2, 3]
    scores = [segment.score for segment in most_replayed]
    assert scores == sorted(scores, reverse=True)
    assert most_replayed[0].start_seconds < most_replayed[0].end_seconds


def test_normalization_preserves_the_video_identity(recorded_dump: dict[str, Any]) -> None:
    metadata = normalize(recorded_dump)

    assert metadata.video_id == "dQw4w9WgXcQ"
    assert metadata.title.startswith("Rick Astley")
    assert metadata.channel_id == recorded_dump["channel_id"]


def test_the_tag_list_survives_normalization(recorded_dump: dict[str, Any]) -> None:
    # The headline field. The official Data API returns snippet.tags only to
    # the video's owner, so for anyone else these exist nowhere else.
    metadata = normalize(recorded_dump)

    assert len(metadata.tags) == 27
    assert "rick astley" in metadata.tags


def test_the_exact_upload_instant_becomes_an_aware_utc_datetime(
    recorded_dump: dict[str, Any],
) -> None:
    # yt-dlp gives both a unix `timestamp` and a coarse `upload_date`. The
    # instant is the part worth having, and a naive datetime would silently
    # take on whatever timezone the reader assumes.
    metadata = normalize(recorded_dump)

    assert metadata.published_at is not None
    assert metadata.published_at.tzinfo is not None
    assert metadata.published_at.isoformat().startswith("2009-10-25")


def test_chapters_are_carried_through_in_playback_order(
    chaptered_dump: dict[str, Any],
) -> None:
    # The empty-list test above passes against a hardcoded [], which would lose
    # every chapter of every video that has them. This is the test that makes
    # the mapping real.
    chapters = normalize(chaptered_dump).chapters

    assert len(chapters) == 11
    assert chapters[0].title == "Linux Kernel Architecture"
    assert chapters[0].start_seconds == 0
    assert chapters[0].end_seconds == 15
    starts = [chapter.start_seconds for chapter in chapters]
    assert starts == sorted(starts)


def test_the_engagement_counts_are_carried_through(recorded_dump: dict[str, Any]) -> None:
    # The comment harvest deliberately does not report a total, because yt-dlp
    # overwrites comment_count when getcomments is on. This is where the real
    # number lives, and that claim is only true if this test passes.
    metadata = normalize(recorded_dump)

    assert metadata.view_count == recorded_dump["view_count"]
    assert metadata.like_count == recorded_dump["like_count"]
    assert metadata.comment_count == 2_400_000


def test_the_descriptive_fields_are_carried_through(recorded_dump: dict[str, Any]) -> None:
    metadata = normalize(recorded_dump)

    assert metadata.channel == recorded_dump["channel"]
    assert metadata.duration_seconds == recorded_dump["duration"]
    assert metadata.description
    assert metadata.categories == ["Music"]


def test_a_video_reports_which_caption_languages_exist_without_their_urls(
    recorded_dump: dict[str, Any],
) -> None:
    # Listing the tracks is useful; storing their URLs is not. They are signed
    # and short-lived, so a stored one is a guaranteed 403 later.
    metadata = normalize(recorded_dump)

    manual = [track for track in metadata.caption_tracks if not track.is_automatic]
    assert {track.language for track in manual} == {"de-DE", "en", "es-419", "ja", "pt-BR"}
    assert len(metadata.caption_tracks) > 100
    assert not any("url" in track.model_dump() for track in metadata.caption_tracks)


def test_the_upload_date_survives_when_the_exact_instant_does_not() -> None:
    """YouTube stopped returning `timestamp` for at least some videos.

    Observed live on 2026-08-18: three consecutive extractions of a video
    whose recorded dump carries timestamp=1256453853 came back with
    timestamp=None and upload_date='20091025'. The instant is the field worth
    having and it is genuinely sometimes absent, so dropping the coarse date
    on the floor loses the publication date entirely rather than losing
    precision.
    """
    dump = {"id": "dQw4w9WgXcQ", "title": "x", "upload_date": "20091025"}

    metadata = normalize(dump)

    assert metadata.published_at is None
    assert metadata.published_date is not None
    assert metadata.published_date.isoformat() == "2009-10-25"


def test_the_exact_instant_is_still_preferred_when_it_is_there(
    recorded_dump: dict[str, Any],
) -> None:
    # The recorded dump predates the change, which is why it is kept: a parser
    # that stopped handling the older shape would break on every fixture and
    # on every video YouTube still answers fully for.
    assert recorded_dump["timestamp"]

    metadata = normalize(recorded_dump)

    assert metadata.published_at is not None
    assert metadata.published_date is not None
