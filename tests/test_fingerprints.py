"""What makes two collections the same question.

The cache is only as trustworthy as this: two spellings of one request that
fingerprint differently produce two artifacts and halve the hit rate, and two
genuinely different requests that fingerprint the same serve one the other's
answer.
"""

from __future__ import annotations

from tubedepth.fingerprints import fingerprint


def test_the_same_request_fingerprints_the_same_way() -> None:
    first = fingerprint(kind="video.metadata", target="dQw4w9WgXcQ", schema_version="1")
    second = fingerprint(kind="video.metadata", target="dQw4w9WgXcQ", schema_version="1")

    assert first == second


def test_a_different_target_fingerprints_differently() -> None:
    metadata = fingerprint(kind="video.metadata", target="dQw4w9WgXcQ", schema_version="1")
    other = fingerprint(kind="video.metadata", target="kJQP7kiw5Fk", schema_version="1")

    assert metadata != other


def test_a_different_kind_for_one_video_fingerprints_differently() -> None:
    metadata = fingerprint(kind="video.metadata", target="dQw4w9WgXcQ", schema_version="1")
    transcript = fingerprint(kind="video.transcript", target="dQw4w9WgXcQ", schema_version="1")

    assert metadata != transcript


def test_changing_the_schema_version_invalidates_the_fingerprint() -> None:
    """The part that stops a normalizer change serving stale shapes.

    Without it, adding a field to VideoMetadata leaves every cached artifact
    looking fresh while missing the field, and the only symptom is data that
    is quietly a version behind.
    """
    before = fingerprint(kind="video.metadata", target="dQw4w9WgXcQ", schema_version="1")
    after = fingerprint(kind="video.metadata", target="dQw4w9WgXcQ", schema_version="2")

    assert before != after


def test_parameters_are_part_of_the_question() -> None:
    # "The top forty comments" and "the newest forty" are different answers to
    # different questions, and serving one for the other is silently wrong.
    top = fingerprint(
        kind="video.comments",
        target="dQw4w9WgXcQ",
        schema_version="1",
        parameters={"sort": "top", "limit": 40},
    )
    newest = fingerprint(
        kind="video.comments",
        target="dQw4w9WgXcQ",
        schema_version="1",
        parameters={"sort": "new", "limit": 40},
    )

    assert top != newest


def test_parameter_order_does_not_change_the_fingerprint() -> None:
    # Two callers writing the same options in a different order are asking the
    # same question, and a cache that disagrees does the work twice.
    one = fingerprint(
        kind="video.comments",
        target="x",
        schema_version="1",
        parameters={"sort": "top", "limit": 40},
    )
    other = fingerprint(
        kind="video.comments",
        target="x",
        schema_version="1",
        parameters={"limit": 40, "sort": "top"},
    )

    assert one == other
