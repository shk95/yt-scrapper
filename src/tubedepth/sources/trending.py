"""What YouTube itself calls popular, which nothing here can derive.

The trending *page* was retired — `/feed/trending` redirects to the home page,
and `listings.py` records that there is no ranking left to scrape. The Data
API's `chart=mostPopular` outlived it, checked on 2026-08-20: 200 results for
each of KR and US.

This is the only source in the project that reports a ranking rather than an
observation. Everything else here can be differentiated into a trend by
collecting it twice; YouTube's own ordering cannot be reconstructed from any
number of samples, because it is not a function of anything we can see.

**It spends a different budget, and that is the point.** One request is one
quota unit against 10,000 a day, so a five-minute cadence is about 288 units
and the per-address YouTube tolerance that caps everything else here is
untouched. The thing that makes trend polling dangerous — sustained load on the
one constrained resource — does not apply.
"""

from __future__ import annotations

import os
import re
from datetime import timedelta
from typing import Any

import httpx
from pydantic import BaseModel

from ..egress.control import Lane
from ..egress.transport import Egress
from ..errors import ConfigurationError, ExtractionError, RateLimitedError, UpstreamError
from ..identifiers import TargetType
from ..schemas import ListedVideo, VideoListing
from .registry import SourceCost
from .ytdlp_runtime import YtdlpRuntime

CHART_URL = "https://www.googleapis.com/youtube/v3/videos"
API_KEY_VARIABLE = "TUBEDEPTH_DATA_API_KEY"

# One request per 50 results, and the chart holds 200. Four requests is four
# quota units, which is nothing against the daily allowance — the cap here is
# about not pretending to a longer ranking than YouTube publishes.
PAGE_SIZE = 50
DEFAULT_LIMIT = 200

_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def duration_seconds(value: str | None) -> int | None:
    """`PT10M30S` as seconds, or None for anything unrecognised.

    Returned as None rather than 0 when it does not parse: a live broadcast has
    no duration and reporting zero would make it the shortest video in the
    chart wherever anyone sorted.
    """
    if not value:
        return None
    matched = _DURATION.match(value)
    if matched is None:
        return None
    parts = {name: int(found or 0) for name, found in matched.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def _count(statistics: dict[str, Any], key: str) -> int | None:
    """A count the API returns as a string, or None where it withholds one.

    `viewCount` is absent for a video whose uploader hides it. Absent and zero
    are different facts, and only one of them is about the video.
    """
    raw = statistics.get(key)
    return int(raw) if isinstance(raw, str) and raw.isdigit() else None


def parse_chart(body: dict[str, Any], *, region: str) -> VideoListing:
    """The chart as a listing, in the order it arrived.

    The order is the entire value of this source, so nothing here sorts. A
    listing rather than a shape of its own, because trending *is* a ranked list
    of videos — and reusing the model means `--then` already works, so one
    queued region becomes a metadata job per trending video.
    """
    videos: list[ListedVideo] = []
    skipped = 0
    for item in body.get("items") or []:
        video_id = item.get("id")
        if not isinstance(video_id, str) or not video_id:
            # An entry with no id cannot be collected or counted as one.
            skipped += 1
            continue
        snippet = item.get("snippet") or {}
        videos.append(
            ListedVideo(
                video_id=video_id,
                title=snippet.get("title"),
                duration_seconds=duration_seconds(
                    (item.get("contentDetails") or {}).get("duration")
                ),
                view_count=_count(item.get("statistics") or {}, "viewCount"),
                channel=snippet.get("channelTitle"),
                channel_id=snippet.get("channelId"),
            )
        )
    return VideoListing(
        source_kind=TrendingVideosSource.kind,
        listing_id=region,
        videos=videos,
        skipped_count=skipped,
    )


def _reasons(body: dict[str, Any]) -> set[str]:
    errors = (body.get("error") or {}).get("errors") or []
    return {
        str(entry["reason"]) for entry in errors if isinstance(entry, dict) and entry.get("reason")
    }


class TrendingVideosSource:
    kind = "trending.videos"
    target_type = TargetType.REGION
    lane = Lane.YOUTUBE_DATA_API
    cost = SourceCost.CHEAP
    schema_version = "1"
    payload_model: type[BaseModel] = VideoListing
    # The chart is recomputed continuously and the whole point is what is
    # rising now. Cheap in quota and off the constrained budget, so this can be
    # short without competing with anything.
    default_freshness = timedelta(minutes=15)

    def __init__(self, *, limit: int = DEFAULT_LIMIT) -> None:
        self._limit = limit
        # In the cache key: a top-50 chart is not an answer to a request for
        # the top 200.
        self.cache_parameters = {"limit": limit}

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> VideoListing:
        key = os.environ.get(API_KEY_VARIABLE)
        if not key:
            # Configuration, not upstream. Retrying will never help, and the
            # rate controller must not read a missing key as a route in
            # trouble and quarantine an address over it.
            raise ConfigurationError(
                f"{API_KEY_VARIABLE} is not set, so the trending chart cannot be read"
            )

        collected: list[dict[str, Any]] = []
        page: str | None = None
        followed: set[str] = set()
        with egress.http_client() as client:
            while len(collected) < self._limit:
                body = self._page(client, target, key, page, len(collected))
                items = body.get("items")
                collected += items or []
                page = body.get("nextPageToken")
                if not page:
                    break
                # A token promises another page, and the loop condition alone
                # would follow it forever. Two guards, because the API has
                # shipped both failure shapes: a page that adds nothing, and a
                # token that repeats. Each lap is a quota unit, spent while
                # holding the job's lease.
                if not items:
                    if not isinstance(items, list):
                        # No `items` list at all is not the chart running
                        # short — it is a response this parser no longer
                        # understands, and returning what was collected so far
                        # would be the silent short listing that stays
                        # deployed for weeks.
                        raise ExtractionError(
                            f"the trending chart answered with a nextPageToken and no items"
                            f" list: {target}"
                        )
                    # `items: []` is the API saying the chart is empty of its
                    # own accord — end of chart, not an error: the chart
                    # legitimately runs short of the limit.
                    break
                if page in followed:
                    break
                followed.add(page)
        return parse_chart({"items": collected[: self._limit]}, region=target)

    def _page(
        self, client: httpx.Client, region: str, key: str, page: str | None, have: int
    ) -> dict[str, Any]:
        parameters = {
            "part": "snippet,contentDetails,statistics",
            "chart": "mostPopular",
            "regionCode": region,
            "maxResults": str(min(PAGE_SIZE, self._limit - have)),
            "key": key,
        }
        if page:
            parameters["pageToken"] = page
        try:
            response = client.get(CHART_URL, params=parameters)
        except httpx.HTTPError as error:
            raise UpstreamError(f"the trending chart could not be reached: {region}") from error

        if response.status_code == httpx.codes.OK:
            return dict(response.json())

        body = self._body(response)
        message = (body.get("error") or {}).get("message") or response.text[:200]
        if response.status_code == httpx.codes.FORBIDDEN and _reasons(body) & {
            "quotaExceeded",
            "rateLimitExceeded",
            "userRateLimitExceeded",
        }:
            # A spent quota and a disabled key are both 403 and want opposite
            # responses: one is waiting, the other is a person fixing
            # something. Only the first is the controller's business.
            raise RateLimitedError(f"the data api quota is spent: {message}")
        if response.status_code == httpx.codes.FORBIDDEN:
            # The other 403. A key the project has not enabled, or one that has
            # been disabled, answers the same way to every retry — so reporting
            # it as an upstream failure spends quota units to be told the same
            # thing three times, and describes a route as being in trouble when
            # the trouble is a setting nobody has changed.
            raise ConfigurationError(f"the data api refused this key: {message}")
        raise UpstreamError(
            f"the trending chart refused {region} ({response.status_code}): {message}"
        )

    @staticmethod
    def _body(response: httpx.Response) -> dict[str, Any]:
        try:
            return dict(response.json())
        except ValueError:
            return {}
