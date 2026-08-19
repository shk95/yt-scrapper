"""Turning yt-dlp's one exception into the several things it actually means.

yt-dlp reports everything as `DownloadError` with the reason in the message:
a private video, a members-only video, a network blip and a bot check all
arrive identically. Nothing here translated them, so all four escaped as
non-domain exceptions — logged as "unexpected failure", terminal, and
deliberately neutral to the rate controller.

That is right for three of the four and badly wrong for the fourth. A bot check
is the single signal the whole adaptive controller exists to react to, and it
was the one thing that never reached it.
"""

from __future__ import annotations

import pytest
from yt_dlp.utils import DownloadError

from tubedepth.errors import RateLimitedError, UnavailableError, UpstreamError
from tubedepth.sources.ytdlp_runtime import error_for_download_failure

BOT_CHECK = (
    "ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you're not a bot. "
    "Use --cookies-from-browser or --cookies for the authentication."
)


def test_a_bot_check_is_reported_as_a_rate_limit_so_the_controller_reacts() -> None:
    """The only failure that is genuinely about the address we went out from."""
    translated = error_for_download_failure(DownloadError(BOT_CHECK), target="dQw4w9WgXcQ")

    assert isinstance(translated, RateLimitedError)


def test_a_video_that_cannot_be_watched_is_not_the_routes_fault() -> None:
    for message in (
        "ERROR: [youtube] x: Private video. Sign in if you've been granted access to this video",
        "ERROR: [youtube] x: Video unavailable",
        # Both phrasings occur. Seen on consecutive live runs: an invalid id
        # gives the first, a real but withdrawn video gives the second.
        "ERROR: [youtube] x: This video is unavailable",
        "ERROR: [youtube] x: This video is available to this channel's members on level: Tier 1",
        "ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate.",
        "ERROR: [youtube] x: The uploader has not made this video available in your country",
    ):
        translated = error_for_download_failure(DownloadError(message), target="x")
        assert isinstance(translated, UnavailableError), message
        assert not translated.retryable, message


def test_an_unrecognised_failure_stays_retryable_upstream() -> None:
    """Unknown means unknown: worth one more go, and no verdict about the line."""
    translated = error_for_download_failure(
        DownloadError("ERROR: unable to download video data: HTTP Error 500"), target="x"
    )

    assert isinstance(translated, UpstreamError)
    assert not isinstance(translated, RateLimitedError)
    assert translated.retryable


def test_the_message_keeps_the_reason_and_the_target() -> None:
    translated = error_for_download_failure(DownloadError(BOT_CHECK), target="dQw4w9WgXcQ")

    assert "dQw4w9WgXcQ" in str(translated)
    assert "bot" in str(translated)


def test_the_runtime_translates_rather_than_leaking_yt_dlps_exception() -> None:
    """The seam is the runtime, so no source has to know yt-dlp's error shape."""
    from tubedepth.egress.transport import DirectEgress
    from tubedepth.sources.ytdlp_runtime import LibraryYtdlpRuntime

    class BotCheckedRuntime(LibraryYtdlpRuntime):
        def _run(self, target: str, options: dict[str, object]) -> object:
            raise DownloadError(BOT_CHECK)

    with pytest.raises(RateLimitedError):
        BotCheckedRuntime().extract("dQw4w9WgXcQ", egress=DirectEgress())
