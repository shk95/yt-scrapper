"""The only place a transport is built.

An egress is one exit address. Its whole job is to answer "what proxy, if any"
exactly once and hand that same answer to both transports this project uses,
so they cannot disagree. A disagreement is invisible — nothing errors and
nothing logs — and it leaks the origin address on whichever one was forgotten.

Sync rather than async, deliberately, and a departure from the original design.
The work is IO-bound at roughly one job per second, so a thread per job gets
the same concurrency as an event loop with none of the machinery, and it keeps
sources callable from the synchronous service layer the CLI already uses. The
moment that stops being true is the moment to revisit it — not before.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

import httpx

from ..errors import UpstreamError

# Measured, not chosen for taste: returnyoutubedislikeapi.com answers 403 to
# urllib's default agent and 200 to a browser one. Setting it on the client
# means no source can forget it.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT_SECONDS = 30.0


@runtime_checkable
class Egress(Protocol):
    name: str
    proxy_url: str | None

    def http_client(self) -> httpx.Client: ...
    def ytdlp_options(self) -> dict[str, Any]: ...
    def fetch(self, url: str, *, headers: Mapping[str, str] | None = None) -> bytes: ...


class _BaseEgress:
    name: str
    proxy_url: str | None

    def http_client(self) -> httpx.Client:
        return httpx.Client(
            proxy=self.proxy_url,
            headers={"User-Agent": BROWSER_USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
        )

    def ytdlp_options(self) -> dict[str, Any]:
        """The yt-dlp side of the same decision.

        Absent rather than None when there is no proxy: yt-dlp treats an
        explicit empty proxy as "disable proxying", which is not the same
        thing as saying nothing.
        """
        options: dict[str, Any] = {"http_headers": {"User-Agent": BROWSER_USER_AGENT}}
        if self.proxy_url is not None:
            options["proxy"] = self.proxy_url
        return options

    def fetch(self, url: str, *, headers: Mapping[str, str] | None = None) -> bytes:
        try:
            with self.http_client() as client:
                response = client.get(url, headers=dict(headers or {}))
                response.raise_for_status()
                return response.content
        except httpx.HTTPStatusError as error:
            raise UpstreamError(
                f"{url} answered {error.response.status_code} on egress {self.name}"
            ) from error
        except httpx.HTTPError as error:
            raise UpstreamError(f"{url} could not be reached on egress {self.name}") from error


class DirectEgress(_BaseEgress):
    """This host's own line. Currently the best route for YouTube."""

    def __init__(self, name: str = "direct") -> None:
        self.name = name
        self.proxy_url: str | None = None


class ProxiedEgress(_BaseEgress):
    """Anything reached through a proxy — wireproxy, Gluetun, or a vendor."""

    def __init__(self, *, name: str, proxy_url: str) -> None:
        self.name = name
        self.proxy_url: str | None = proxy_url
