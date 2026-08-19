"""What must be stripped from a dump before it can be committed.

This is the code that keeps credentials out of git, so it is worth more than
the convenience it looks like. A raw `yt-dlp --dump-json` carries ~37 signed
googlevideo URLs and one signed caption URL per language; all of them expire
within hours, all of them bloat the diff, and gitleaks reads them as secrets.
"""

from __future__ import annotations

import json
from typing import Any

from tubedepth.fixture_capture import (
    REDACTED_AVATAR_URL,
    REDACTED_CAPTION_URL,
    redact_for_fixture,
)


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


def test_comment_author_identity_is_anonymized() -> None:
    """Commit the shape, not the people.

    A comment carries a display name, a channel id, a profile URL and an avatar
    URL. All four are personal data, all four belong to someone who did not
    agree to appear in this repository, and git history keeps them after the
    file is deleted. Nothing under test needs the identity — only that there is
    one, and that it is stable enough to thread replies by.
    """
    dump: dict[str, Any] = {
        "comments": [
            {
                "id": "Ugz1",
                "parent": "root",
                "text": "can confirm: he never gave us up",
                "author": "@SomeRealPerson",
                "author_id": "UCBR8-60-B28hp2BmDPdntcQ",
                "author_url": "https://www.youtube.com/@SomeRealPerson",
                "author_thumbnail": "https://yt3.ggpht.com/abc123=s88-c-k-c0x00ffffff-no-rj",
                "author_is_verified": True,
                "is_pinned": True,
                "like_count": 300000,
            }
        ]
    }

    comment = redact_for_fixture(dump)["comments"][0]

    assert "SomeRealPerson" not in json.dumps(comment)
    assert "UCBR8-60-B28hp2BmDPdntcQ" not in json.dumps(comment)
    assert comment["author"].startswith("@author")
    assert comment["author_thumbnail"] == REDACTED_AVATAR_URL
    # The parts the tests actually read must survive untouched.
    assert comment["text"] == "can confirm: he never gave us up"
    assert comment["author_is_verified"] is True
    assert comment["is_pinned"] is True
    assert comment["like_count"] == 300000


def test_the_same_author_gets_the_same_pseudonym_across_comments() -> None:
    # Threading and "did the uploader reply to themselves" both depend on being
    # able to tell two comments apart by author. A fresh pseudonym per comment
    # would destroy exactly the property the fixture exists to exercise.
    dump: dict[str, Any] = {
        "comments": [
            {"id": "a", "author": "@same", "author_id": "UC_same"},
            {"id": "b", "author": "@other", "author_id": "UC_other"},
            {"id": "c", "author": "@same", "author_id": "UC_same"},
        ]
    }

    comments = redact_for_fixture(dump)["comments"]

    assert comments[0]["author_id"] == comments[2]["author_id"]
    assert comments[0]["author_id"] != comments[1]["author_id"]


def test_an_innertube_response_has_its_signed_media_urls_replaced() -> None:
    """Found by the hygiene guard, not by foresight.

    An InnerTube response embeds signed googlevideo URLs of its own, arriving
    by a different route than the yt-dlp dump they are stripped from. Two
    committed fixtures carried them before the guard failed the build.
    """
    from tubedepth.fixture_capture import redact_innertube_response

    payload = {
        "contents": {
            "player": {"url": "https://rr3---sn-x.googlevideo.com/videoplayback?sig=secret"},
            "title": "kept",
        },
        "trackingParams": "should-not-survive",
    }

    redacted = redact_innertube_response(payload)

    assert "googlevideo" not in json.dumps(redacted)
    assert "trackingParams" not in redacted
    assert redacted["contents"]["title"] == "kept"
