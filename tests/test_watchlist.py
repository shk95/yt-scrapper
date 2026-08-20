"""The typed watch list, parsed.

The format exists to make a mistake loud. A bare-id list cannot tell a channel
handle from a search query, and a typo in it collects nothing while reporting
success — so every assertion here is either "this line means exactly that job"
or "this line is refused, and the refusal says which line".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tubedepth.errors import ValidationError
from tubedepth.watchlist import read_watchlist


def written(tmp_path: Path, body: str, name: str = "watchlist.txt") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_each_directive_becomes_the_kind_and_follow_up_it_names(tmp_path: Path) -> None:
    """The whole table, in one test, because the table is the format.

    A directive that mapped to the wrong kind would be a watch list collecting
    something other than what it says — and nothing downstream could notice,
    since every kind here is a real kind.
    """
    path = written(
        tmp_path,
        "video    dQw4w9WgXcQ\nchannel  @director_pihyunjung\nsearch   kpop debut\ntrending KR\n",
    )

    directives = read_watchlist(path)

    assert [(one.kind, one.target, one.follow_up) for one in directives] == [
        ("video.metadata", "dQw4w9WgXcQ", None),
        ("channel.videos", "@director_pihyunjung", "video.metadata"),
        ("search.videos", "kpop debut", "video.metadata"),
        ("trending.videos", "KR", "video.metadata"),
    ]


def test_a_search_query_keeps_the_spaces_inside_it(tmp_path: Path) -> None:
    """The target is the rest of the line, not the next word.

    Splitting on every run of whitespace would turn `search kpop debut` into a
    query for `kpop` and a stray word, which is a different search that still
    returns results — the failure would be invisible in the queue.
    """
    path = written(tmp_path, "search 케이팝 데뷔\n")

    assert [one.target for one in read_watchlist(path)] == ["케이팝 데뷔"]


def test_a_hash_inside_a_search_query_is_part_of_the_query(tmp_path: Path) -> None:
    """There is no inline comment syntax, on purpose.

    Hashtags are how a real query is written, so a parser that treated `#` as
    the start of a comment anywhere on the line would silently truncate the
    most ordinary search anyone would put in this file.
    """
    path = written(tmp_path, "search #shorts recap\n")

    assert [one.target for one in read_watchlist(path)] == ["#shorts recap"]


def test_blank_lines_and_comments_are_skipped_without_disturbing_the_numbering(
    tmp_path: Path,
) -> None:
    """A line number in an error message is only useful if it is the real one.

    Counting only the lines that carry a directive is the mistake that makes an
    error point three lines above the typo, in a file the operator is reading
    in an editor that numbers every line.
    """
    path = written(
        tmp_path,
        "# the channel being watched\n"
        "\n"
        "channel @director_pihyunjung\n"
        "\n"
        "   # indented comments are comments too\n"
        "video dQw4w9WgXcQ\n",
    )

    directives = read_watchlist(path)

    assert [(one.kind, one.line) for one in directives] == [
        ("channel.videos", 3),
        ("video.metadata", 6),
    ]


def test_an_unknown_directive_is_refused_and_the_message_names_the_line(
    tmp_path: Path,
) -> None:
    """A typo that collects nothing quietly is what this format exists to stop.

    The known directives are named in the message as well: an operator who
    wrote `channels` is one character from being right and should not have to
    find the documentation to learn which character.
    """
    path = written(tmp_path, "video dQw4w9WgXcQ\nchannels @director_pihyunjung\n")

    with pytest.raises(ValidationError) as raised:
        read_watchlist(path)

    message = str(raised.value)
    assert "line 2" in message, message
    assert "channels" in message, message
    assert "trending" in message, "the refusal did not say which directives are known"


def test_a_directive_with_nothing_after_it_is_refused(tmp_path: Path) -> None:
    """`channel` on its own is an unfinished edit, not a channel."""
    path = written(tmp_path, "video dQw4w9WgXcQ\n\nchannel\n")

    with pytest.raises(ValidationError) as raised:
        read_watchlist(path)

    assert "line 3" in str(raised.value), str(raised.value)


def test_a_watch_list_that_is_not_there_is_refused_and_the_message_names_it(
    tmp_path: Path,
) -> None:
    """Same rule the bare-id reader already followed: unreadable is not empty.

    A timer firing hourly at a file somebody moved would otherwise queue
    nothing, report success, and leave the series to stop moving with no
    failure anywhere for anyone to notice.
    """
    missing = tmp_path / "gone.txt"

    with pytest.raises(ValidationError) as raised:
        read_watchlist(missing)

    assert "gone.txt" in str(raised.value), str(raised.value)


def test_the_directive_is_matched_whatever_case_it_is_written_in(tmp_path: Path) -> None:
    path = written(tmp_path, "Video dQw4w9WgXcQ\nTRENDING KR\n")

    assert [one.kind for one in read_watchlist(path)] == ["video.metadata", "trending.videos"]


def test_tabs_and_stray_whitespace_around_a_line_do_not_change_what_it_means(
    tmp_path: Path,
) -> None:
    """The file is edited by hand, so it is indented and tab-separated by hand.

    The target keeps its own interior spacing and loses only what surrounds it;
    normalising the target itself is `normalize_target`'s job at enqueue time,
    not the parser's.
    """
    path = written(tmp_path, "\t channel\t\t@director_pihyunjung  \n  search \t kpop  debut \t\n")

    directives = read_watchlist(path)

    assert [(one.kind, one.target) for one in directives] == [
        ("channel.videos", "@director_pihyunjung"),
        ("search.videos", "kpop  debut"),
    ]


def test_an_empty_list_is_an_empty_list(tmp_path: Path) -> None:
    """A file of nothing but comments parses; refusing it belongs to the caller,
    which knows whether an empty pass is worth complaining about."""
    path = written(tmp_path, "# nothing yet\n\n")

    assert read_watchlist(path) == []
