"""Enumerating what to collect.

Until these exist, every video id has to be typed by hand, which is the thing
that actually stops large-scale collection — before throughput does. Channels,
searches and playlists all come back in the same shape, so one normalizer
serves all three.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from tubedepth.sources.listings import normalize

FIXTURES = Path(__file__).parent / "fixtures/ytdlp/listings"


def load(name: str) -> dict[str, Any]:
    with gzip.open(FIXTURES / f"2026-08-18-{name}.json.gz", "rt", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def channel_listing() -> dict[str, Any]:
    return load("channel-videos")


@pytest.fixture
def search_listing() -> dict[str, Any]:
    return load("search")


@pytest.fixture
def playlist_listing() -> dict[str, Any]:
    return load("playlist")


def test_a_channel_listing_yields_every_video_it_carried(
    channel_listing: dict[str, Any],
) -> None:
    listing = normalize(channel_listing, source_kind="channel.videos")

    assert len(listing.videos) == 12
    assert all(len(video.video_id) == 11 for video in listing.videos)
    assert listing.videos[0].title


def test_a_listing_carries_the_counts_that_make_it_worth_filtering_on(
    channel_listing: dict[str, Any],
) -> None:
    # The point of a listing is deciding what to collect in full. Duration and
    # view count are what anyone filters on, and they arrive free here — a
    # per-video metadata job to learn them would cost one request each.
    listing = normalize(channel_listing, source_kind="channel.videos")

    assert any(video.duration_seconds for video in listing.videos)
    assert any(video.view_count for video in listing.videos)


def test_a_search_listing_reports_what_was_searched_for(
    search_listing: dict[str, Any],
) -> None:
    listing = normalize(search_listing, source_kind="search.videos")

    assert len(listing.videos) == 8
    assert listing.source_kind == "search.videos"


def test_a_playlist_listing_keeps_the_playlist_order(
    playlist_listing: dict[str, Any],
) -> None:
    # A playlist is an ordered thing; sorting it would destroy the only
    # information a playlist carries beyond its membership.
    listing = normalize(playlist_listing, source_kind="playlist.items")

    raw_order = [entry["id"] for entry in playlist_listing["entries"]]
    assert [video.video_id for video in listing.videos] == raw_order


def test_an_unavailable_entry_is_dropped_rather_than_carried_as_a_hole() -> None:
    # yt-dlp leaves a placeholder for deleted and private videos. Carrying one
    # through would queue a job that can only ever fail.
    payload = {
        "entries": [
            {"id": "dQw4w9WgXcQ", "title": "fine"},
            {"id": None, "title": "[Deleted video]"},
            {"title": "[Private video]"},
        ]
    }

    listing = normalize(payload, source_kind="playlist.items")

    assert [video.video_id for video in listing.videos] == ["dQw4w9WgXcQ"]
    assert listing.skipped_count == 2
