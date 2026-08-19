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


def test_a_source_that_declares_no_parameters_keeps_the_fingerprint_it_always_had() -> None:
    """A frozen literal, and the strongest guard available here.

    Wiring parameters into the cache key moves the fingerprint of every source
    that declares any. It must move the fingerprint of no source that declares
    none, or every artifact of the other kinds is orphaned at once — a silent
    re-collection of the whole store against the one budget that caps this
    system. `fingerprint()` writes `"parameters": {}` unconditionally, so this
    holds by construction; the literal is what says so if the canonical JSON is
    ever rearranged.
    """
    assert (
        fingerprint(kind="video.metadata", target="dQw4w9WgXcQ", schema_version="1")
        == "c329421f06a4c24aaec4b19fc7985f29fad80fbe06c16211680fc7489b7690b5"
    )


def test_declaring_no_parameters_and_declaring_an_empty_mapping_are_the_same_question() -> None:
    """The identity the blast radius and the backfill both rest on."""
    assert fingerprint(kind="k", target="t", schema_version="1") == fingerprint(
        kind="k", target="t", schema_version="1", parameters={}
    )


def test_a_different_limit_is_a_different_question() -> None:
    """The failure issue #2 is actually about.

    Raise the listing cap and re-run a channel swept an hour ago, and without
    this the cached 100-item listing is served for the request that asked for
    1,000. Nothing errors; the sweep looks like it worked and is missing 900.
    """
    assert fingerprint(
        kind="channel.videos", target="@someone", schema_version="1", parameters={"limit": 100}
    ) != fingerprint(
        kind="channel.videos", target="@someone", schema_version="1", parameters={"limit": 1000}
    )
