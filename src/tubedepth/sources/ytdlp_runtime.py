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
from yt_dlp.utils import DownloadError

from ..egress.transport import Egress
from ..errors import RateLimitedError, TubedepthError, UnavailableError, UpstreamError

# What yt-dlp says when YouTube wants proof we are a person. It is the only
# message here that is about the address rather than the video, and the whole
# adaptive controller exists to react to it — so it has to arrive as something
# the controller recognises rather than as an unexpected exception.
BOT_CHECK_MARKERS = (
    "sign in to confirm you're not a bot",
    "confirm you're not a bot",
)

# The video exists and we may not have it. No retry helps, and none of these
# say anything about the line the request went out on.
UNAVAILABLE_MARKERS = (
    "private video",
    # Both phrasings occur, on consecutive live runs: an invalid id gives the
    # first, a real but withdrawn video gives the second.
    "video unavailable",
    "video is unavailable",
    "this video is available to this channel's members",
    "join this channel to get access",
    "sign in to confirm your age",
    "age-restricted",
    "not made this video available in your country",
    "video has been removed",
    "account associated with this video has been terminated",
    "this live event has ended",
)


def error_for_download_failure(error: DownloadError, *, target: str) -> TubedepthError:
    """Turn yt-dlp's single exception into the several things it means.

    yt-dlp reports a private video, a members-only video, a network blip and a
    bot check as the same `DownloadError` with the reason in the message. Left
    untranslated they all escaped as non-domain exceptions: logged as
    "unexpected failure", terminal, and neutral to the rate controller.

    That is right for three of the four. It is badly wrong for the bot check,
    which is the one failure that is genuinely evidence about the address —
    and the one thing that never reached the controller that exists for it.

    Unknown messages stay `UpstreamError`: retryable, and no claim about the
    route. Guessing from an unrecognised message is how a working address gets
    quarantined by a message nobody has read yet.
    """
    message = str(error).lower()
    if any(marker in message for marker in BOT_CHECK_MARKERS):
        return RateLimitedError(f"youtube asked for proof we are not a bot, fetching: {target}")
    if any(marker in message for marker in UNAVAILABLE_MARKERS):
        return UnavailableError(f"video cannot be watched from here: {target} ({error})")
    return UpstreamError(f"yt-dlp failed for: {target} ({error})")


@runtime_checkable
class YtdlpRuntime(Protocol):
    def extract(
        self,
        target: str,
        *,
        egress: Egress,
        options: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


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

    def _run(self, target: str, options: dict[str, Any]) -> Any:
        """The one call that touches yt-dlp. Separated so a test can replace it."""
        with YoutubeDL(options) as downloader:  # type: ignore[arg-type]
            return downloader.extract_info(target, download=False)

    def extract(
        self,
        target: str,
        *,
        egress: Egress,
        options: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        # yt-dlp types its params as a TypedDict, which a plain dict cannot be
        # proven to satisfy. The shape is right; the checker cannot see it.
        # Order matters: the source's options come last so a source can raise
        # a limit the base set caps, and the egress sits in the middle because
        # no source may override which address it leaves from.
        merged = self.BASE_OPTIONS | egress.ytdlp_options() | dict(options or {})
        try:
            info = self._run(target, merged)
        except DownloadError as error:
            # Translated here so no source has to know yt-dlp's error shape,
            # and so the bot check reaches the rate controller as something it
            # recognises rather than as an unexpected exception.
            raise error_for_download_failure(error, target=target) from error
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
