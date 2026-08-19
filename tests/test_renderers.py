"""Reading InnerTube responses without pretending to understand their shape.

This is the most fragile code in the project and the tests are shaped around
that. YouTube reshuffles the containers around a renderer far more often than
it renames the renderer itself, so nothing here reads a fixed path — and the
renames that do happen must be loud rather than silent.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from tubedepth.errors import ExtractionError
from tubedepth.innertube.renderers import collect, find_all, flatten_text, observed_renderers

FIXTURES = Path(__file__).parent / "fixtures/innertube"


def load(name: str) -> dict[str, Any]:
    with gzip.open(FIXTURES / f"2026-08-18-{name}.json.gz", "rt", encoding="utf-8") as handle:
        return json.load(handle)


def test_a_renderer_is_found_wherever_it_sits() -> None:
    # By name, at any depth. A fixed path is the part that breaks first,
    # because the containers move more often than the renderers are renamed.
    payload = {"a": {"b": [{"c": {"targetRenderer": {"value": 1}}}]}}

    assert [found["value"] for found in find_all(payload, "targetRenderer")] == [1]


def test_the_related_videos_fixture_carries_the_renderer_the_parser_expects() -> None:
    found = list(find_all(load("next-related"), "lockupViewModel"))

    assert len(found) >= 10


def test_text_is_read_out_of_every_shape_youtube_uses() -> None:
    """Four shapes, all live in the same response today.

    simpleText is the legacy one, runs is the common one, content belongs to
    the viewModel family, and a bare string turns up in newer surfaces.
    """
    assert flatten_text({"simpleText": "one"}) == "one"
    assert flatten_text({"runs": [{"text": "one "}, {"text": "two"}]}) == "one two"
    assert flatten_text({"content": "one"}) == "one"
    assert flatten_text("one") == "one"
    assert flatten_text(None) is None


def test_collecting_a_renderer_that_is_present_returns_it() -> None:
    payload = {"items": [{"wantedRenderer": {"n": 1}}, {"wantedRenderer": {"n": 2}}]}

    found = collect(payload, accepted=("wantedRenderer",), contract="wanted")

    assert [entry["n"] for entry in found] == [1, 2]


def test_an_older_renderer_name_still_works() -> None:
    # The old name stays in the accepted list so a rollback on YouTube's side
    # does not break us in the other direction.
    payload = {"items": [{"compactVideoRenderer": {"n": 1}}]}

    found = collect(
        payload, accepted=("lockupViewModel", "compactVideoRenderer"), contract="related"
    )

    assert len(found) == 1


def test_a_response_that_says_it_is_empty_yields_nothing_without_complaint() -> None:
    # A channel with no community posts is a fact about the channel. YouTube
    # says so with a message renderer, and that is the difference between
    # "nothing here" and "we cannot read this any more".
    payload = {"contents": {"messageRenderer": {"text": {"simpleText": "No posts"}}}}

    found = collect(
        payload,
        accepted=("backstagePostRenderer",),
        contract="community posts",
        empty_markers=("messageRenderer",),
    )

    assert found == []


def test_a_response_with_neither_the_renderer_nor_an_empty_marker_is_an_error() -> None:
    """The trap this whole module exists to avoid.

    yt-dlp returns an empty list for a community tab it can no longer read, and
    an empty list is indistinguishable from a channel that has no posts. That
    ambiguity is how a broken scraper stays deployed for weeks.
    """
    payload = {"contents": {"someOtherRenderer": {}, "richGridRenderer": {}}}

    with pytest.raises(ExtractionError, match="community posts"):
        collect(
            payload,
            accepted=("backstagePostRenderer",),
            contract="community posts",
            empty_markers=("messageRenderer",),
        )


def test_the_error_names_what_youtube_actually_sent() -> None:
    # The whole diagnosis, in the message: what we wanted and what arrived.
    payload = {"contents": {"richGridRenderer": {}, "shelfRenderer": {}}}

    with pytest.raises(ExtractionError) as raised:
        collect(payload, accepted=("lockupViewModel",), contract="related videos")

    message = str(raised.value)
    assert "lockupViewModel" in message
    assert "richGridRenderer" in message


def test_the_observed_renderers_of_a_real_response_can_be_listed() -> None:
    # What someone runs first when a parser breaks.
    names = observed_renderers(load("browse-community"))

    assert "backstagePostRenderer" in names


def test_a_key_holding_a_list_of_renderers_yields_each_of_them() -> None:
    """Renderers are not always mappings.

    YouTube keeps the channel subscriber string in `metadataParts`, which is a
    list of entries rather than one. A traversal that only descends into
    mappings walks straight past it and reports the field as absent — which is
    how "no subscriber count" gets confused with "we cannot read it".
    """
    payload = {"header": {"metadataParts": [{"text": {"content": "4.53M subscribers"}}]}}

    found = list(find_all(payload, "metadataParts"))

    assert len(found) == 1
    assert flatten_text(found[0]["text"]) == "4.53M subscribers"
