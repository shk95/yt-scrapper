"""The one place yt-dlp is called.

Sources are forbidden from building their own YoutubeDL. yt-dlp is the single
most likely thing here to break when YouTube changes, and the fix is almost
always one extractor argument — so when that day comes there should be exactly
one file to edit, not one per source.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from yt_dlp import YoutubeDL

from ..egress.transport import Egress
from ..errors import UpstreamError


@runtime_checkable
class YtdlpRuntime(Protocol):
    def extract(self, target: str, *, egress: Egress) -> Mapping[str, Any]: ...


class LibraryYtdlpRuntime:
    """Calls yt-dlp in process. Reaches the network."""

    BASE_OPTIONS: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # Politeness is a default, not an afterthought: this is a private tool
        # whose whole strategy is being unremarkable in the logs.
        "sleep_requests": 0.75,
        "retries": 3,
    }

    def extract(self, target: str, *, egress: Egress) -> Mapping[str, Any]:
        # yt-dlp types its params as a TypedDict, which a plain dict cannot be
        # proven to satisfy. The shape is right; the checker cannot see it.
        options = self.BASE_OPTIONS | egress.ytdlp_options()
        with YoutubeDL(options) as downloader:  # type: ignore[arg-type]
            info = downloader.extract_info(target, download=False)
            if info is None:
                # Documented as possible and reachable in practice, so it gets a
                # domain error rather than an AttributeError three frames later.
                raise UpstreamError(f"yt-dlp returned nothing for: {target}")
            # sanitize_info drops the private keys and non-serializable objects
            # extract_info leaves behind. Without it the dict looks fine right
            # up until json.dumps. It is typed as possibly returning None too,
            # so the check covers both rather than only the obvious one.
            sanitized = YoutubeDL.sanitize_info(info)
            if sanitized is None:
                raise UpstreamError(f"yt-dlp returned nothing usable for: {target}")
            return sanitized
