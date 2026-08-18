"""POSTing to youtubei/v1.

No API key. Most published advice still says one is required; measured on
2026-08-18, a WEB-context request with a browser User-Agent and no `key`
parameter answers 200.

Every request leaves through an egress, like everything else, so the proxy
decision is made in one place and cannot be forgotten here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from ..egress.transport import Egress
from ..errors import RateLimitedError, UpstreamError

BASE_URL = "https://www.youtube.com/youtubei/v1"
CLIENT_CONTEXT: dict[str, Any] = {
    "client": {
        "clientName": "WEB",
        "clientVersion": "2.20240726.00.00",
        "hl": "en",
        "gl": "US",
    }
}


class InnerTubeClient:
    def __init__(self, egress: Egress) -> None:
        self._egress = egress

    def call(self, endpoint: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = {"context": CLIENT_CONTEXT, **body}
        try:
            with self._egress.http_client() as client:
                response = client.post(
                    f"{BASE_URL}/{endpoint}",
                    params={"prettyPrint": "false"},
                    content=json.dumps(payload),
                    headers={"Content-Type": "application/json"},
                )
        except httpx.HTTPError as error:
            raise UpstreamError(f"innertube {endpoint} could not be reached") from error

        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise RateLimitedError(f"innertube refused {endpoint}, rate limited")
        if response.is_error:
            raise UpstreamError(f"innertube {endpoint} answered {response.status_code}")
        return response.json()
