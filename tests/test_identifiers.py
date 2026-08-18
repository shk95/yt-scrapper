"""Turning whatever a caller pasted into the identifier YouTube uses."""

from __future__ import annotations

import pytest

from tubedepth.errors import ValidationError
from tubedepth.identifiers import normalize_video_identifier


def test_a_watch_url_and_a_bare_identifier_normalize_to_the_same_video_id() -> None:
    assert (
        normalize_video_identifier("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    )
    assert normalize_video_identifier("dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_a_youtu_be_short_link_yields_the_same_video_id() -> None:
    assert normalize_video_identifier("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_a_value_that_is_not_a_video_identifier_is_rejected() -> None:
    with pytest.raises(ValidationError, match="video identifier is not valid: too-short"):
        normalize_video_identifier("too-short")


def test_a_youtube_url_without_a_video_parameter_is_rejected_as_a_domain_error() -> None:
    # Not a pedantic case: any bare channel or home URL reaching this function
    # would otherwise surface a KeyError, which escapes the taxonomy and lands
    # on a client as a 500 instead of a 4xx.
    with pytest.raises(ValidationError):
        normalize_video_identifier("https://www.youtube.com/")


def test_a_shorts_url_yields_the_video_id() -> None:
    assert normalize_video_identifier("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_the_embed_live_and_v_url_forms_yield_the_video_id() -> None:
    for url in (
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
        "https://www.youtube.com/v/dQw4w9WgXcQ",
    ):
        assert normalize_video_identifier(url) == "dQw4w9WgXcQ", url
