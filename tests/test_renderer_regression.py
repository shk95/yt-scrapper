"""The regression net for the fragile half.

What this proves and what it does not is worth being precise about. It proves
our parsers have not regressed against responses YouTube gave on a known date.
It proves nothing about what YouTube is sending now — only `just contract`
does that, and only on a connection YouTube does not challenge.

The mutation tests are the important ones. A regression suite that only ever
sees a passing fixture cannot tell you it would catch a rename.
"""

from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from tubedepth.errors import ExtractionError
from tubedepth.innertube.parsers import (
    parse_channel_about,
    parse_community_posts,
    parse_related_videos,
)

FIXTURES = Path(__file__).parent / "fixtures/innertube"


def load(name: str) -> dict[str, Any]:
    with gzip.open(FIXTURES / f"2026-08-18-{name}.json.gz", "rt", encoding="utf-8") as handle:
        return json.load(handle)


def rename_renderer(payload: dict[str, Any], old: str, new: str) -> dict[str, Any]:
    """What a YouTube rename looks like, applied to a recording."""
    text = json.dumps(payload).replace(f'"{old}"', f'"{new}"')
    return json.loads(text)


def test_related_videos_parse_from_the_recorded_watch_next_response() -> None:
    related = parse_related_videos(load("next-related"), video_id="dQw4w9WgXcQ")

    assert len(related.items) >= 10
    first = related.items[0]
    assert len(first.video_id) == 11
    assert first.title
    # Recorded on the payload so a stored result carries evidence of which
    # parse produced it.
    assert related.renderer_shape == "lockupViewModel"


def test_a_renamed_related_renderer_raises_rather_than_returning_nothing() -> None:
    """The mutation that proves this suite would catch a rename.

    Without it the fixture only ever demonstrates the happy path, and a parser
    that silently returned [] would pass every test in this file.
    """
    mutated = rename_renderer(load("next-related"), "lockupViewModel", "lockupViewModelV2")

    with pytest.raises(ExtractionError, match="related videos"):
        parse_related_videos(mutated, video_id="dQw4w9WgXcQ")


def test_community_posts_parse_from_the_recorded_browse_response() -> None:
    posts = parse_community_posts(load("browse-community"), channel_id="UCuAXFkgsw1L7xaCfnd5JJOw")

    assert posts.posts
    assert any(post.text for post in posts.posts)


def test_a_renamed_community_renderer_raises_rather_than_returning_nothing() -> None:
    # This is the exact failure yt-dlp has: an empty list for a tab it can no
    # longer read, indistinguishable from a channel with no posts.
    mutated = rename_renderer(
        load("browse-community"), "backstagePostRenderer", "backstagePostRendererV2"
    )

    with pytest.raises(ExtractionError, match="community posts"):
        parse_community_posts(mutated, channel_id="UCuAXFkgsw1L7xaCfnd5JJOw")


def test_a_channel_with_no_community_posts_parses_to_an_empty_list() -> None:
    # A fact about the channel, not a parser failure. YouTube says so with a
    # message renderer, and that marker is what separates the two.
    payload = {"contents": {"messageRenderer": {"text": {"simpleText": "No posts yet"}}}}

    posts = parse_community_posts(payload, channel_id="UC_empty")

    assert posts.posts == []


def test_channel_about_reads_what_the_recorded_response_carries() -> None:
    about = parse_channel_about(load("browse-channel-home"), channel_id="UCuAXFkgsw1L7xaCfnd5JJOw")

    assert about.channel_id == "UCuAXFkgsw1L7xaCfnd5JJOw"
    # Rounded, and named so. YouTube publishes "4.53M subscribers" and nothing
    # more precise exists anywhere.
    assert about.subscriber_count_text is not None
    assert "subscriber" in about.subscriber_count_text.lower()


def test_the_about_parser_never_reports_an_exact_subscriber_count() -> None:
    """There is no such number to report.

    YouTube publishes a rounded string and the Data API rounds too. A field
    promising an exact count would be a lie the type system cannot catch, so
    the model does not have one.
    """
    about = parse_channel_about(load("browse-channel-home"), channel_id="UC_x")

    assert not hasattr(about, "subscriber_count")
    assert hasattr(about, "subscriber_count_approximate")


@pytest.mark.parametrize("name", sorted(path.name for path in FIXTURES.glob("2026-*.json.gz")))
def test_every_recorded_response_still_parses(name: str) -> None:
    """Parametrized over the directory, so a new fixture adds coverage for free."""
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)

    if "related" in name:
        assert parse_related_videos(payload, video_id="x").items
    elif "community" in name:
        assert parse_community_posts(payload, channel_id="x") is not None
    else:
        assert parse_channel_about(payload, channel_id="x") is not None


def test_the_mutation_helper_does_not_alter_the_fixture_on_disk() -> None:
    # A mutation test that edited the recording would quietly corrupt every
    # other test in this file.
    before = copy.deepcopy(load("next-related"))
    rename_renderer(before, "lockupViewModel", "somethingElse")

    assert load("next-related") == before
