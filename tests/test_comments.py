"""Normalizing a comment harvest.

The raw shape carries two names that mean something other than what they say
and one sentinel string, and it arrives flat with the thread encoded in a
parent pointer. All three are worth fixing once here rather than in every
caller.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from tubedepth.sources.comments import normalize

FIXTURE = Path(__file__).parent / "fixtures/ytdlp/comments/2026-08-18-dQw4w9WgXcQ-top40.json.gz"


@pytest.fixture
def harvest() -> dict[str, Any]:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def test_a_top_level_comment_has_no_parent_identifier(harvest: dict[str, Any]) -> None:
    # yt-dlp writes the string "root". A sentinel that looks like an id is a
    # bug waiting for someone to compare against it.
    assert any(comment["parent"] == "root" for comment in harvest["comments"])

    collected = normalize(harvest, sort="top")

    top_level = [comment for comment in collected.comments if comment.parent_id is None]
    assert top_level
    assert all(comment.parent_id != "root" for comment in collected.comments)


def test_every_reply_points_at_a_comment_in_the_same_harvest(
    harvest: dict[str, Any],
) -> None:
    # The whole reason to keep the list flat: threading has to be
    # reconstructable, and a dangling parent silently drops a subtree.
    collected = normalize(harvest, sort="top")

    identifiers = {comment.comment_id for comment in collected.comments}
    dangling = [
        comment.comment_id
        for comment in collected.comments
        if comment.parent_id is not None and comment.parent_id not in identifiers
    ]
    assert dangling == []


def test_a_hearted_comment_is_named_for_what_youtube_shows(
    harvest: dict[str, Any],
) -> None:
    # yt-dlp calls it `is_favorited`. What YouTube shows is a heart from the
    # channel, and "favorited" reads like something the viewer did.
    assert any(comment.get("is_favorited") for comment in harvest["comments"])

    collected = normalize(harvest, sort="top")

    assert any(comment.is_hearted_by_uploader for comment in collected.comments)


def test_the_pinned_and_verified_flags_survive(harvest: dict[str, Any]) -> None:
    collected = normalize(harvest, sort="top")

    assert any(comment.is_pinned for comment in collected.comments)
    assert any(comment.author_is_verified for comment in collected.comments)


def test_comment_timestamps_become_aware_datetimes(harvest: dict[str, Any]) -> None:
    collected = normalize(harvest, sort="top")

    timed = [comment for comment in collected.comments if comment.published_at is not None]
    assert timed
    for comment in timed:
        assert comment.published_at is not None
        assert comment.published_at.tzinfo is not None


def test_a_harvest_stopped_by_a_limit_says_so(harvest: dict[str, Any]) -> None:
    # The difference between "this video has 40 comments" and "we stopped at
    # 40" is the difference between data and a misleading number, and only the
    # harvester knows which happened.
    collected = normalize(harvest, sort="top", requested_limit=40)

    assert collected.retrieved_count == 40
    assert collected.is_truncated is True


def test_a_harvest_that_ran_to_the_end_is_not_marked_truncated(
    harvest: dict[str, Any],
) -> None:
    collected = normalize(harvest, sort="top", requested_limit=500)

    assert collected.is_truncated is False


def test_the_harvest_does_not_claim_a_total_it_cannot_know(harvest: dict[str, Any]) -> None:
    """A field that claims one number and holds another is worse than none.

    With getcomments on, yt-dlp overwrites `comment_count` with the number it
    retrieved. This video has 2.4 million comments and the dump says 40.
    Carrying that through as a "reported total" would present our own count as
    YouTube's, and it would be believed. The video's real comment count is on
    video.metadata, where it is not overwritten.
    """
    assert harvest["comment_count"] == len(harvest["comments"])

    collected = normalize(harvest, sort="top")

    assert not hasattr(collected, "reported_total")
