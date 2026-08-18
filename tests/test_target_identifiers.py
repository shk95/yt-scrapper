"""Normalizing the things that are not videos.

A channel handle, a playlist id and a search query are all "targets", and
none of them survive being run through the video normalizer. Which normalizer
applies is a property of the source, not of the caller.
"""

from __future__ import annotations

import pytest

from tubedepth.errors import ValidationError
from tubedepth.identifiers import (
    TargetType,
    normalize_channel_identifier,
    normalize_playlist_identifier,
    normalize_target,
)


def test_a_channel_handle_and_its_url_normalize_to_the_same_handle() -> None:
    assert normalize_channel_identifier("@RickAstleyYT") == "@RickAstleyYT"
    assert normalize_channel_identifier("https://www.youtube.com/@RickAstleyYT") == "@RickAstleyYT"


def test_a_channel_id_and_its_url_normalize_to_the_same_id() -> None:
    channel = "UCuAXFkgsw1L7xaCfnd5JJOw"
    assert normalize_channel_identifier(channel) == channel
    assert normalize_channel_identifier(f"https://www.youtube.com/channel/{channel}") == channel


def test_a_bare_word_is_not_a_channel() -> None:
    # Without this, "music" is accepted as a channel and the failure surfaces
    # as an extractor error minutes later instead of at the edge.
    with pytest.raises(ValidationError, match="channel identifier is not valid"):
        normalize_channel_identifier("music")


def test_a_playlist_id_survives_every_url_form_it_appears_in() -> None:
    playlist = "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"
    assert normalize_playlist_identifier(playlist) == playlist
    assert (
        normalize_playlist_identifier(f"https://www.youtube.com/playlist?list={playlist}")
        == playlist
    )
    # A playlist link handed out from inside a video carries both ids.
    assert (
        normalize_playlist_identifier(
            f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={playlist}"
        )
        == playlist
    )


def test_a_search_query_is_passed_through_untouched() -> None:
    # The one target type with no canonical form. Normalizing it would change
    # what was searched for.
    assert normalize_target(TargetType.QUERY, "  linux kernel  ") == "linux kernel"


def test_an_empty_search_query_is_rejected() -> None:
    with pytest.raises(ValidationError, match="search query is empty"):
        normalize_target(TargetType.QUERY, "   ")


def test_normalize_target_dispatches_on_the_target_type() -> None:
    assert normalize_target(TargetType.VIDEO, "https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert normalize_target(TargetType.CHANNEL, "@RickAstleyYT") == "@RickAstleyYT"
