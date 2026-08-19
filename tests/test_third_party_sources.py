"""SponsorBlock: the one source that never touches YouTube.

That is the whole reason it is worth its own lane. Its cost comes out of
somebody else's budget, so the per-address YouTube tolerance — which is what
actually caps this project — is untouched by it.

There were two. Return YouTube Dislike was removed deliberately; see
`docs/status.md` for why, and `git log -- src/tubedepth/sources/dislikes.py`
for the code if it is ever wanted back.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from tubedepth.egress.transport import DirectEgress
from tubedepth.errors import UpstreamError
from tubedepth.sources.sponsorblock import SponsorBlockSource

FIXTURES = Path(__file__).parent / "fixtures/http"


def load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


@respx.mock
def test_sponsor_segments_are_normalized_with_their_boundaries_in_seconds() -> None:
    respx.get(url__startswith="https://sponsor.ajay.app/api/skipSegments").respond(
        200, json=load("sponsorblock-200.json")
    )

    segments = SponsorBlockSource().collect("rh7oegwCyRk", DirectEgress(), None)  # type: ignore[arg-type]

    assert len(segments.segments) == 4
    first = segments.segments[0]
    assert first.category == "sponsor"
    assert first.start_seconds == pytest.approx(87.973)
    assert first.end_seconds == pytest.approx(100.734)
    assert first.votes is not None


@respx.mock
def test_a_video_with_no_sponsor_segments_is_a_result_and_not_a_failure() -> None:
    """404 is this service's way of saying "nobody has submitted any".

    Treating it as an error would fail a job for every clean video, which is
    most of them.
    """
    respx.get(url__startswith="https://sponsor.ajay.app/api/skipSegments").respond(404)

    segments = SponsorBlockSource().collect("jNQXAC9IVRw", DirectEgress(), None)  # type: ignore[arg-type]

    assert segments.segments == []
    assert segments.video_id == "jNQXAC9IVRw"


@respx.mock
def test_a_sponsorblock_outage_is_still_a_failure() -> None:
    # The distinction that matters: "nobody submitted any" is an answer, "the
    # service is down" is not, and collapsing them hides a real outage.
    respx.get(url__startswith="https://sponsor.ajay.app/api/skipSegments").respond(503)

    with pytest.raises(UpstreamError):
        SponsorBlockSource().collect("rh7oegwCyRk", DirectEgress(), None)  # type: ignore[arg-type]


@respx.mock
def test_a_transport_failure_reaches_the_caller_as_a_domain_error() -> None:
    respx.get(url__startswith="https://sponsor.ajay.app/api/skipSegments").mock(
        side_effect=httpx.ConnectError("refused")
    )

    with pytest.raises(UpstreamError):
        SponsorBlockSource().collect("rh7oegwCyRk", DirectEgress(), None)  # type: ignore[arg-type]
