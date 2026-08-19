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

from pathlib import Path

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


def test_a_configured_cookie_jar_reaches_yt_dlp(tmp_path: Path) -> None:
    """docs/troubleshooting.md prescribes this as the first rung of the ladder.

    Nothing read the variable, so an operator locked out by the bot check
    exported a jar, set it, restarted the worker, and every request went out
    byte-identical. The conclusion available to them was that cookies do not
    help — and the next rung is a different egress, which on this host means a
    datacenter address that the README says is expected to make the YouTube
    lane worse.
    """
    from tubedepth.egress.transport import DirectEgress
    from tubedepth.sources.ytdlp_runtime import LibraryYtdlpRuntime

    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    seen: dict[str, object] = {}

    class Capturing(LibraryYtdlpRuntime):
        def _run(self, target: str, options: dict[str, object]) -> object:
            seen.update(options)
            return {"id": target}

    Capturing(cookies_file=jar).extract("dQw4w9WgXcQ", egress=DirectEgress())

    assert seen["cookiefile"] == str(jar)


def test_a_cookie_jar_that_is_not_there_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    """Silently ignoring it is the bug, one layer along.

    A path with a typo in it would otherwise behave exactly like the version
    that read nothing at all, and the operator would draw the same wrong
    conclusion from the same absent evidence.
    """
    from tubedepth.errors import ConfigurationError
    from tubedepth.sources.ytdlp_runtime import LibraryYtdlpRuntime

    with pytest.raises(ConfigurationError, match="cookie"):
        LibraryYtdlpRuntime(cookies_file=tmp_path / "not-there.txt")


def test_no_cookie_jar_configured_sends_no_cookie_option() -> None:
    """The default is unchanged: nothing about the request moves."""
    from tubedepth.egress.transport import DirectEgress
    from tubedepth.sources.ytdlp_runtime import LibraryYtdlpRuntime

    seen: dict[str, object] = {}

    class Capturing(LibraryYtdlpRuntime):
        def _run(self, target: str, options: dict[str, object]) -> object:
            seen.update(options)
            return {"id": target}

    Capturing().extract("dQw4w9WgXcQ", egress=DirectEgress())

    assert "cookiefile" not in seen
