"""What YouTube itself calls popular, which nothing here can derive.

The trending *page* was retired and `/feed/trending` redirects to the home
page, so there is no ranking left to scrape. The Data API's `chart=mostPopular`
outlived it — checked on 2026-08-20, 200 results for each of KR and US — and it
is the only source of a number this project cannot compute from its own
observations.

It also spends a different budget. Google's quota is 10,000 units a day and a
request costs one; the per-address YouTube tolerance that caps everything else
here is untouched. That is why this has its own lane rather than riding on
`Lane.YOUTUBE`, where a quarantine on one would throttle the other for no
reason.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tubedepth.egress.transport import DirectEgress
from tubedepth.errors import ConfigurationError, ExtractionError, RateLimitedError, UpstreamError
from tubedepth.identifiers import TargetType, normalize_target
from tubedepth.sources.trending import TrendingVideosSource

FIXTURE = Path(__file__).parent / "fixtures/dataapi/2026-08-20-mostpopular-kr.json.gz"


def recorded() -> dict[str, Any]:
    return json.loads(gzip.decompress(FIXTURE.read_bytes()))


def answering(payload: Any, status: int = 200) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handle)


class FakeEgress(DirectEgress):
    """A DirectEgress whose client answers from a recording."""

    def __init__(self, transport: httpx.MockTransport) -> None:
        super().__init__()
        self._transport = transport

    def http_client(self) -> httpx.Client:  # type: ignore[override]
        return httpx.Client(transport=self._transport)


class PagedTransport:
    """Answers requests from a list of page bodies, and refuses to run away.

    The last page is repeated for any request past the end — which is exactly
    what a pagination bug does to the real API — and the cap turns an infinite
    loop into a test failure instead of a hang.
    """

    def __init__(self, pages: list[dict[str, Any]], cap: int = 6) -> None:
        self.pages = pages
        self.cap = cap
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls > self.cap:
            raise AssertionError(f"pagination did not terminate within {self.cap} requests")
        body = self.pages[min(self.calls - 1, len(self.pages) - 1)]
        return httpx.Response(200, json=body)


def paged(pages: list[dict[str, Any]], cap: int = 6) -> tuple[FakeEgress, PagedTransport]:
    handler = PagedTransport(pages, cap)
    return FakeEgress(httpx.MockTransport(handler)), handler


def test_a_region_code_is_normalised_to_the_shape_the_api_wants() -> None:
    """`kr`, `KR` and ` kr ` are one region, and the API only accepts one of them."""
    assert normalize_target(TargetType.REGION, " kr ") == "KR"


def test_something_that_is_not_a_region_code_is_refused_before_a_request() -> None:
    """A three-letter code is the plausible mistake — ISO 3166 has both, and
    this endpoint takes only alpha-2. Refusing here costs nothing; refusing
    after the request spends a quota unit to be told the same thing."""
    from tubedepth.errors import ValidationError

    with pytest.raises(ValidationError):
        normalize_target(TargetType.REGION, "KOR")


def test_the_chart_is_read_as_a_ranked_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `VideoListing` rather than a shape of its own.

    Trending is a ranked list of videos, which is what that model already is —
    and reusing it means `--then` works, so one queued region becomes a
    metadata job per trending video without anything new.
    """
    monkeypatch.setenv("TUBEDEPTH_DATA_API_KEY", "test-key")
    source = TrendingVideosSource()

    listing = source.collect("KR", FakeEgress(answering(recorded())), runtime=None)  # type: ignore[arg-type]

    assert listing.source_kind == "trending.videos"
    assert listing.listing_id == "KR"
    assert len(listing.videos) == 3
    first = listing.videos[0]
    assert first.video_id and first.title
    assert first.view_count and first.view_count > 0
    assert first.duration_seconds and first.duration_seconds > 0


def test_the_rank_is_the_order_the_chart_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole value of this source is YouTube's own ordering, so it is kept
    rather than sorted by anything of ours."""
    monkeypatch.setenv("TUBEDEPTH_DATA_API_KEY", "test-key")
    body = recorded()
    expected = [item["id"] for item in body["items"]]

    listing = TrendingVideosSource().collect("KR", FakeEgress(answering(body)), runtime=None)  # type: ignore[arg-type]

    assert [video.video_id for video in listing.videos] == expected


def test_no_api_key_is_a_configuration_problem_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not an upstream failure: retrying it forever would not help, and the
    rate controller must not read a missing key as a route in trouble."""
    monkeypatch.delenv("TUBEDEPTH_DATA_API_KEY", raising=False)

    with pytest.raises(ConfigurationError, match="TUBEDEPTH_DATA_API_KEY"):
        TrendingVideosSource().collect("KR", FakeEgress(answering({})), runtime=None)  # type: ignore[arg-type]


def test_a_spent_quota_reaches_the_controller_as_a_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google answers 403 for a spent quota, which is a route being refused
    rather than a bad request — and the controller can only back off if it is
    told in the vocabulary it understands."""
    monkeypatch.setenv("TUBEDEPTH_DATA_API_KEY", "test-key")
    spent = {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "quota"}}

    with pytest.raises(RateLimitedError):
        TrendingVideosSource().collect("KR", FakeEgress(answering(spent, 403)), runtime=None)  # type: ignore[arg-type]


def test_a_key_the_api_refuses_is_a_setting_and_not_a_route_in_trouble(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled key and a spent quota are both 403 and want opposite answers.

    A spent quota is waiting. A key the project has not enabled answers the
    same way to every retry — so an `UpstreamError`, which is retryable and
    maps to `Verdict.THROTTLED`, would spend quota units to be told the same
    thing three times *and* narrow the lane's own window. The earlier version
    of this test asserted only that it was not a rate limit, which was true and
    weaker than what should hold.
    """
    from tubedepth.errors import ConfigurationError

    monkeypatch.setenv("TUBEDEPTH_DATA_API_KEY", "test-key")
    refused = {"error": {"errors": [{"reason": "accessNotConfigured"}], "message": "off"}}

    with pytest.raises(ConfigurationError):
        TrendingVideosSource().collect("KR", FakeEgress(answering(refused, 403)), runtime=None)  # type: ignore[arg-type]

    assert not issubclass(ConfigurationError, RateLimitedError)
    assert not issubclass(ConfigurationError, UpstreamError)


def test_a_second_page_is_fetched_when_the_first_has_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary two-page walk, pinned so the loop guards below cannot be
    satisfied by simply refusing to paginate at all."""
    monkeypatch.setenv("TUBEDEPTH_DATA_API_KEY", "test-key")
    egress, handler = paged(
        [
            {"items": [{"id": "v1"}, {"id": "v2"}], "nextPageToken": "PAGE-2"},
            {"items": [{"id": "v3"}]},
        ]
    )

    listing = TrendingVideosSource().collect("KR", egress, runtime=None)  # type: ignore[arg-type]

    assert [video.video_id for video in listing.videos] == ["v1", "v2", "v3"]
    assert handler.calls == 2


def test_an_empty_page_with_a_token_is_the_end_of_the_chart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`items: []` with a truthy `nextPageToken` must end the walk with what
    was collected — the chart legitimately runs short of the limit — rather
    than following the token forever at one quota unit per lap."""
    monkeypatch.setenv("TUBEDEPTH_DATA_API_KEY", "test-key")
    egress, handler = paged(
        [
            {"items": [{"id": "v1"}, {"id": "v2"}], "nextPageToken": "PAGE-2"},
            {"items": [], "nextPageToken": "PAGE-3"},
        ]
    )

    listing = TrendingVideosSource().collect("KR", egress, runtime=None)  # type: ignore[arg-type]

    assert [video.video_id for video in listing.videos] == ["v1", "v2"]
    assert handler.calls == 2


def test_a_next_page_token_that_repeats_ends_the_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token already followed cannot be followed again: pages that keep
    echoing the same token would otherwise be fetched until the limit filled
    with duplicates."""
    monkeypatch.setenv("TUBEDEPTH_DATA_API_KEY", "test-key")
    egress, handler = paged(
        [
            {"items": [{"id": "v1"}], "nextPageToken": "SAME"},
            {"items": [{"id": "v2"}], "nextPageToken": "SAME"},
        ]
    )

    listing = TrendingVideosSource().collect("KR", egress, runtime=None)  # type: ignore[arg-type]

    assert [video.video_id for video in listing.videos] == ["v1", "v2"]
    assert handler.calls == 2


def test_a_page_with_a_token_but_no_items_list_is_a_parser_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ending the chart early is only allowed for a page that *says* it is
    empty (`items: []`). A body carrying a `nextPageToken` and no `items` list
    at all is a response the parser no longer understands, and turning it into
    a silent short listing is how a broken scraper stays deployed for weeks."""
    monkeypatch.setenv("TUBEDEPTH_DATA_API_KEY", "test-key")
    egress, _ = paged([{"nextPageToken": "PAGE-2"}])

    with pytest.raises(ExtractionError):
        TrendingVideosSource().collect("KR", egress, runtime=None)  # type: ignore[arg-type]
