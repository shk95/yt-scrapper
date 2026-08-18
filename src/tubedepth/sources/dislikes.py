"""Dislike estimates from Return YouTube Dislike.

YouTube removed the public dislike count in 2021 and the Data API followed, so
there is no official source at all. This one reconstructs it, and the numbers
are labelled as reconstructions everywhere they appear.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
from pydantic import BaseModel

from ..egress.control import Lane
from ..egress.transport import Egress
from ..errors import RateLimitedError, UpstreamError
from ..identifiers import TargetType
from ..schemas import DislikeEstimate
from .registry import SourceCost
from .ytdlp_runtime import YtdlpRuntime

VOTES_URL = "https://returnyoutubedislikeapi.com/votes"


class DislikesSource:
    kind = "video.dislikes"
    target_type = TargetType.VIDEO
    # Its own lane, and that is the point of it: the cost comes out of
    # somebody else's budget, so the per-address YouTube tolerance — which is
    # what actually caps this project — is untouched.
    lane = Lane.RYD
    cost = SourceCost.CHEAP
    schema_version = "1"
    payload_model: type[BaseModel] = DislikeEstimate
    # The service's own estimate moves slowly, and it documents 100 requests a
    # minute and 10,000 a day, so asking often is both pointless and rude.
    default_freshness = timedelta(hours=24)

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> DislikeEstimate:
        try:
            with egress.http_client() as client:
                response = client.get(VOTES_URL, params={"videoId": target})
                if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
                    raise RateLimitedError(f"returnyoutubedislike refused, rate limited: {target}")
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as error:
            raise UpstreamError(
                f"returnyoutubedislike answered {error.response.status_code} for: {target}"
            ) from error
        except httpx.HTTPError as error:
            raise UpstreamError(f"returnyoutubedislike could not be reached: {target}") from error

        return DislikeEstimate(
            video_id=body.get("id", target),
            likes=body.get("likes"),
            dislikes=body.get("dislikes"),
            rating=body.get("rating"),
            view_count=body.get("viewCount"),
        )
