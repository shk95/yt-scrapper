"""Sponsor and self-promotion segments from SponsorBlock.

Community-submitted, published under CC BY-NC-SA 4.0 — redistributing this
data carries attribution and non-commercial terms that are the operator's to
honour.
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
from pydantic import BaseModel

from ..egress.control import Lane
from ..egress.transport import Egress
from ..errors import RateLimitedError, UpstreamError
from ..identifiers import TargetType
from ..schemas import SponsorSegment, SponsorSegments
from .registry import SourceCost
from .ytdlp_runtime import YtdlpRuntime

SEGMENTS_URL = "https://sponsor.ajay.app/api/skipSegments"
CATEGORIES = (
    "sponsor",
    "intro",
    "outro",
    "selfpromo",
    "interaction",
    "music_offtopic",
)


class SponsorBlockSource:
    kind = "video.sponsor_segments"
    target_type = TargetType.VIDEO
    lane = Lane.SPONSORBLOCK
    cost = SourceCost.CHEAP
    schema_version = "1"
    payload_model: type[BaseModel] = SponsorSegments
    # Submissions land within hours of an upload and then settle.
    default_freshness = timedelta(hours=6)

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> SponsorSegments:
        try:
            with egress.http_client() as client:
                response = client.get(
                    SEGMENTS_URL,
                    params={"videoID": target, "categories": json.dumps(list(CATEGORIES))},
                )
        except httpx.HTTPError as error:
            raise UpstreamError(f"sponsorblock could not be reached: {target}") from error

        if response.status_code == httpx.codes.NOT_FOUND:
            # This service's way of saying nobody has submitted a segment,
            # which is true of most videos. Treating it as an error would fail
            # a job for every clean video.
            return SponsorSegments(video_id=target, segments=[])
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise RateLimitedError(f"sponsorblock refused, rate limited: {target}")
        if response.is_error:
            # An outage is not the same answer as "none submitted", and
            # collapsing the two hides the outage.
            raise UpstreamError(f"sponsorblock answered {response.status_code} for: {target}")

        return SponsorSegments(
            video_id=target,
            segments=[
                SponsorSegment(
                    category=entry["category"],
                    action_type=entry.get("actionType"),
                    start_seconds=entry["segment"][0],
                    end_seconds=entry["segment"][1],
                    votes=entry.get("votes"),
                    is_locked=bool(entry.get("locked")),
                )
                for entry in response.json()
            ],
        )
