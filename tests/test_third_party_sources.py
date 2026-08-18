"""Two sources that never touch YouTube.

That is the whole reason they are worth having on their own lane: their cost
comes out of somebody else's budget, and the per-address YouTube tolerance —
which is what actually caps this project — is untouched by them.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from tubedepth.egress.control import Lane
from tubedepth.egress.transport import BROWSER_USER_AGENT, DirectEgress
from tubedepth.errors import UpstreamError
from tubedepth.sources.dislikes import DislikesSource
from tubedepth.sources.sponsorblock import SponsorBlockSource

FIXTURES = Path(__file__).parent / "fixtures/http"


def load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


@respx.mock
def test_a_dislike_estimate_is_normalized_from_the_service_response() -> None:
    respx.get(url__startswith="https://returnyoutubedislikeapi.com/votes").respond(
        200, json=load("ryd-200.json")
    )

    estimate = DislikesSource().collect("dQw4w9WgXcQ", DirectEgress(), None)  # type: ignore[arg-type]

    assert estimate.video_id == "dQw4w9WgXcQ"
    assert estimate.dislikes == 517461
    assert estimate.likes == 19338779
    assert estimate.rating == pytest.approx(4.8957585, rel=1e-6)


@respx.mock
def test_the_dislike_count_is_labelled_an_estimate_and_not_a_count() -> None:
    """It is not YouTube's number and must never be presented as one.

    Return YouTube Dislike reconstructs it from an archive plus extension
    telemetry. A field called `dislikes` sitting next to a real `likes` invites
    exactly the wrong reading, so the model says what it is.
    """
    respx.get(url__startswith="https://returnyoutubedislikeapi.com/votes").respond(
        200, json=load("ryd-200.json")
    )

    estimate = DislikesSource().collect("dQw4w9WgXcQ", DirectEgress(), None)  # type: ignore[arg-type]

    assert estimate.source == "returnyoutubedislike"
    assert estimate.is_estimate is True


@respx.mock
def test_the_dislike_request_carries_a_browser_user_agent() -> None:
    # Measured, not assumed: this service answers 403 to urllib's default agent
    # and 200 to a browser one.
    route = respx.get(url__startswith="https://returnyoutubedislikeapi.com/votes").respond(
        200, json=load("ryd-200.json")
    )

    DislikesSource().collect("dQw4w9WgXcQ", DirectEgress(), None)  # type: ignore[arg-type]

    assert route.calls[0].request.headers["user-agent"] == BROWSER_USER_AGENT


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
def test_a_rate_limited_third_party_is_reported_as_such() -> None:
    # Return YouTube Dislike documents 100 requests a minute and 10,000 a day,
    # so 429 is a case this will actually meet rather than a hypothetical.
    from tubedepth.errors import RateLimitedError

    respx.get(url__startswith="https://returnyoutubedislikeapi.com/votes").respond(429)

    with pytest.raises(RateLimitedError):
        DislikesSource().collect("dQw4w9WgXcQ", DirectEgress(), None)  # type: ignore[arg-type]


def test_these_sources_draw_on_their_own_lanes_and_not_on_youtube() -> None:
    # The point of having them: their cost comes out of somebody else's
    # budget, so the per-address YouTube tolerance is untouched.
    assert DislikesSource().lane is Lane.RYD
    assert SponsorBlockSource().lane is Lane.SPONSORBLOCK


@respx.mock
def test_a_transport_failure_reaches_the_caller_as_a_domain_error() -> None:
    respx.get(url__startswith="https://sponsor.ajay.app/api/skipSegments").mock(
        side_effect=httpx.ConnectError("refused")
    )

    with pytest.raises(UpstreamError):
        SponsorBlockSource().collect("rh7oegwCyRk", DirectEgress(), None)  # type: ignore[arg-type]
