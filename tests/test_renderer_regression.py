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
from collections.abc import Mapping
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
    """A recorded response by name, whichever day it was captured.

    Dated filenames say when a recording was made, which matters when YouTube
    changes shape — but a test naming the date has to be edited every time a
    fixture is refreshed, and an edit like that is where an assertion quietly
    loses its meaning.
    """
    matches = sorted(FIXTURES.glob(f"*-{name}.json.gz"))
    if not matches:
        raise FileNotFoundError(f"no fixture recorded for: {name}")
    with gzip.open(matches[-1], "rt", encoding="utf-8") as handle:
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
    about = parse_channel_about(load("browse-channel-about"), channel_id="UCuAXFkgsw1L7xaCfnd5JJOw")

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
    about = parse_channel_about(load("browse-channel-about"), channel_id="UC_x")

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
    elif "channel-about" in name:
        assert parse_channel_about(payload, channel_id="x") is not None
    elif "channel-home" in name:
        # Kept deliberately as the negative case. This response is what the
        # about source used to receive and parse into a wrong answer, so the
        # recording earns its place by proving the refusal rather than a parse.
        with pytest.raises(ExtractionError):
            parse_channel_about(payload, channel_id="x")
    else:
        raise AssertionError(f"no parser claims this fixture: {name}")


def test_the_mutation_helper_does_not_alter_the_fixture_on_disk() -> None:
    # A mutation test that edited the recording would quietly corrupt every
    # other test in this file.
    before = copy.deepcopy(load("next-related"))
    rename_renderer(before, "lockupViewModel", "somethingElse")

    assert load("next-related") == before


def test_channel_about_reads_the_channels_own_description_not_a_videos() -> None:
    """The defect this fixture exists for.

    `channel.about` sent no `params`, so it received the channel *home* tab,
    and the parser — which searches by renderer name — took the first
    `description` in the payload. On a channel with a featured video that is
    the video's description, returned as the channel's. Plausible, wrong, and
    invisible: the old fixture was literally named `browse-channel-home` and
    the test asserted only the fields that happened to be right.

    YouTube has no About *tab* any more; the data lives behind a continuation
    from an engagement panel, which is what this fixture records.
    """
    about = parse_channel_about(load("browse-channel-about"), channel_id="UCuAXFkgsw1L7xaCfnd5JJOw")

    assert about.description is not None
    assert "Raindrops" in about.description, "this is the channel's own description"
    assert "Reflections Tour" not in about.description, "this is a video's description"


def test_channel_about_carries_what_only_innertube_can_give() -> None:
    """Join date, country and links are the reason this source exists.

    yt-dlp does not expose any of them. If they come back empty the source has
    no purpose, so their absence must fail a test rather than pass quietly.
    """
    about = parse_channel_about(load("browse-channel-about"), channel_id="UCuAXFkgsw1L7xaCfnd5JJOw")

    assert about.country == "United Kingdom"
    assert about.joined_text == "Joined Feb 1, 2015"
    assert about.links, "external links are one of the three things this source is for"
    assert any("rickastley" in link for link in about.links)
    assert about.view_count == 2_536_701_615, (
        "the channel's total views, which yt-dlp reports as None"
    )


def test_the_about_parser_refuses_a_response_that_is_not_the_about_panel() -> None:
    """The whole point of the InnerTube contract, applied to this source.

    A home-tab response has renderers and would parse into something. Returning
    that something is exactly how this was broken for a week, so it has to be
    an error rather than a partial answer.
    """
    with pytest.raises(ExtractionError, match="aboutChannelViewModel"):
        parse_channel_about(load("browse-channel-home"), channel_id="UCuAXFkgsw1L7xaCfnd5JJOw")


def test_channel_about_carries_the_name_and_tags_from_the_same_fetch() -> None:
    """What the plan gave `channel.profile` a separate source and request for.

    The channel's name, its keywords and its avatar all sit in
    `channelMetadataRenderer`, which is in the *first* of the two responses
    this source already makes — the one it reads the continuation token from.
    Spending a second extraction on yt-dlp to fetch fields we have already
    been handed would cost a YouTube request per channel to learn nothing new.
    """
    about = parse_channel_about(
        load("browse-channel-about"),
        channel_id="UCuAXFkgsw1L7xaCfnd5JJOw",
        metadata=load("browse-channel-home"),
    )

    assert about.name == "Rick Astley"
    assert about.tags, "channelMetadataRenderer.keywords is the channel's tag list"
    assert any("rick astley" in tag.lower() for tag in about.tags)
    assert about.avatar_url


def test_channel_about_without_the_metadata_response_still_parses() -> None:
    """The about panel is the required half; the metadata is an enrichment."""
    about = parse_channel_about(load("browse-channel-about"), channel_id="UC_x")

    assert about.country == "United Kingdom"
    assert about.name is None
    assert about.tags == []


def test_a_channel_handle_is_resolved_to_a_browse_id_before_browsing() -> None:
    """`@RickAstleyYT` is a legitimate target and InnerTube refuses it.

    The identifier layer accepts handles because YouTube URLs use them, so a
    caller pasting one gets as far as the source before `browse` answers 400 —
    an error about a request nobody made rather than about the handle.
    """
    from tubedepth.sources.innertube_sources import browse_id_for

    resolved: dict[str, Any] = {
        "endpoint": {"browseEndpoint": {"browseId": "UCuAXFkgsw1L7xaCfnd5JJOw"}}
    }

    class Resolving:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Mapping[str, Any]]] = []

        def call(self, endpoint: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
            self.calls.append((endpoint, body))
            return resolved

    client = Resolving()
    assert browse_id_for(client, "@RickAstleyYT") == "UCuAXFkgsw1L7xaCfnd5JJOw"
    assert client.calls[0][0] == "navigation/resolve_url"

    # A channel id needs no round trip and must not spend one.
    untouched = Resolving()
    assert browse_id_for(untouched, "UCuAXFkgsw1L7xaCfnd5JJOw") == "UCuAXFkgsw1L7xaCfnd5JJOw"
    assert untouched.calls == []
