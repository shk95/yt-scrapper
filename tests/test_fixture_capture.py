"""What must be stripped from a dump before it can be committed.

This is the code that keeps credentials out of git, so it is worth more than
the convenience it looks like. A raw `yt-dlp --dump-json` carries ~37 signed
googlevideo URLs and one signed caption URL per language; all of them expire
within hours, all of them bloat the diff, and gitleaks reads them as secrets.
"""

from __future__ import annotations

from typing import Any

from tubedepth.fixture_capture import REDACTED_CAPTION_URL, redact_for_fixture


def test_the_signed_format_urls_are_dropped_entirely() -> None:
    # Nothing in this project reads `formats`, they are the bulk of the bytes,
    # and they are what gitleaks flags. There is no reason to keep the shape.
    dump: dict[str, Any] = {
        "id": "dQw4w9WgXcQ",
        "formats": [{"url": "https://rr3---sn-x.googlevideo.com/videoplayback?sig=secret"}],
    }

    redacted = redact_for_fixture(dump)

    assert "formats" not in redacted
    assert redacted["id"] == "dQw4w9WgXcQ"


def test_a_caption_url_is_replaced_rather_than_removed() -> None:
    # Removing the key loses the shape the transcript source selects on, which
    # would make that source untestable without the network. Replacing keeps
    # the shape and carries nothing that expires.
    dump: dict[str, Any] = {
        "subtitles": {
            "en": [
                {
                    "ext": "json3",
                    "name": "English",
                    "url": "https://www.youtube.com/api/timedtext?v=x&signature=secret&fmt=json3",
                }
            ]
        }
    }

    track = redact_for_fixture(dump)["subtitles"]["en"][0]

    assert track["url"] == REDACTED_CAPTION_URL
    assert track["ext"] == "json3"
    assert track["name"] == "English"


def test_redaction_does_not_mutate_the_dump_it_was_given() -> None:
    # The capture command writes the fixture and then reports on the dump. If
    # redaction edited in place, that report would describe the redacted copy
    # and quietly disagree with what was actually extracted.
    dump: dict[str, Any] = {"formats": [{"url": "https://x.googlevideo.com/a"}]}

    redact_for_fixture(dump)

    assert "formats" in dump
