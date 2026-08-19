"""Subtitles and transcripts, including automatically generated ones.

The official Data API lists caption tracks but will not hand over their text
without the video owner's OAuth, so for anyone else this is the only route.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from ..egress.control import Lane
from ..egress.transport import Egress
from ..errors import NotFoundError, UpstreamError
from ..identifiers import TargetType
from ..schemas import Transcript, TranscriptSegment
from .registry import SourceCost
from .ytdlp_runtime import YtdlpRuntime

# json3 specifically: it is the only format YouTube offers that carries the
# per-segment timings as data rather than as text needing a second parser.
CAPTION_FORMAT = "json3"

# yt-dlp appends this to the automatic-caption key for the language the
# transcription was actually made in; the other 156 are translations of it.
ORIGINAL_SUFFIX = "-orig"

# Used only when the video itself says nothing about what language it is in.
FALLBACK_LANGUAGES = ("ko", "en")

# The query parameter marking an auto-translated track rather than a transcription.
TRANSLATION_MARKER = "tlang="

# yt-dlp files live chat replay under `subtitles`. It is not a caption track.
LIVE_CHAT_KEY = "live_chat"


@dataclass(frozen=True, slots=True)
class CaptionTrack:
    language: str
    name: str | None
    is_automatic: bool
    format: str
    url: str


def _json3_track(tracks: Sequence[Mapping[str, Any]] | None) -> Mapping[str, Any] | None:
    for track in tracks or []:
        if track.get("ext") == CAPTION_FORMAT and track.get("url"):
            return track
    return None


def _track(bucket: Mapping[str, Any], key: str, *, is_automatic: bool) -> CaptionTrack | None:
    found = _json3_track(bucket.get(key))
    if found is None:
        return None
    if TRANSLATION_MARKER in found["url"]:
        # A `tlang=` track is this video's words run through a translator, which
        # this policy never wants, and it draws on a small per-address budget
        # that a sweep exhausts in three or four requests. Filtered here rather
        # than at the call site so no future tier can reach it by accident.
        return None
    return CaptionTrack(
        # The key without yt-dlp's marker: `-orig` says which language the
        # transcription ran in, and is not a language tag anyone should see.
        language=key.removesuffix(ORIGINAL_SUFFIX),
        name=found.get("name"),
        is_automatic=is_automatic,
        format=CAPTION_FORMAT,
        url=found["url"],
    )


def _caption_languages(dump: Mapping[str, Any]) -> set[str]:
    """Every language this video has captions in, in either bucket.

    `live_chat` is excluded: yt-dlp files chat replay under `subtitles` with
    that key, and it is a transcript of the audience rather than of the video.
    """
    keys = {*(dump.get("subtitles") or {}), *(dump.get("automatic_captions") or {})}
    return {key for key in keys if key != LIVE_CHAT_KEY}


def video_language(dump: Mapping[str, Any]) -> str | None:
    """The language the video is actually in, or None if nothing says.

    Two independent signals, and they answer the same question. `language` is
    what yt-dlp reports. The `-orig` key in the automatic captions is what
    YouTube's own transcriber ran in — present whenever there is ASR at all,
    which covers videos yt-dlp leaves as None.

    Both are absent together on old uploads: jNQXAC9IVRw (2005) reports no
    language, has no ASR and carries two manual tracks with nothing to
    distinguish them. That is a real state, not a parsing failure, so it
    returns None and the caller decides.
    """
    reported = dump.get("language")
    if isinstance(reported, str) and reported:
        return reported
    for key in dump.get("automatic_captions") or {}:
        if key.endswith(ORIGINAL_SUFFIX):
            return key.removesuffix(ORIGINAL_SUFFIX)
    return None


def _matching_keys(bucket: Mapping[str, Any], language: str) -> list[str]:
    """Keys for `language`, exact first, then regional variants of it.

    yt-dlp reports `pt` while YouTube lists the track as `pt-BR`, and reports
    `en` for a video whose manual track is `en-US`. Comparing the primary
    subtag is what keeps those from looking like different languages.
    """
    primary = language.partition("-")[0]
    exact = [key for key in bucket if key == language]
    regional = [
        key
        for key in bucket
        if key not in exact
        and not key.endswith(ORIGINAL_SUFFIX)
        and key.partition("-")[0] == primary
    ]
    return exact + sorted(regional)


def caption_track_candidates(
    dump: Mapping[str, Any], *, fallback_languages: Sequence[str] = FALLBACK_LANGUAGES
) -> list[CaptionTrack]:
    """Every track worth trying, best first.

    The policy is the video's own language and nothing else:

    1. Captions a person wrote in that language.
    2. The transcription of it — YouTube's ASR, marked `-orig` when a
       translation of it also exists.

    Translations never appear at either tier. They are two lossy steps from the
    audio, and they are drawn from a budget so small that a sweep spends it in
    three or four requests, after which every video would silently arrive in
    some other language. Refusing them keeps a transcript's language a fact
    about the video rather than a fact about how recently we ran.

    `fallback_languages` applies only when the video's language cannot be
    determined at all. That is rare and real — see `video_language` — and the
    alternative is discarding captions that are sitting right there.

    A list rather than one track because the ranking is a preference and a
    fetch can still be refused; see `TranscriptSource.collect`.

    Raises rather than returning an empty list, and the message says which of
    three failures it is — captions off, a language we will not serve, or a
    fallback that did not match — because they are acted on differently. This
    used to live in a `select_caption_track` wrapper that returned the head,
    and `collect` re-implemented the same two lines because it wants the whole
    ranking. So every test of the selection policy named a function the worker
    never called: change the policy there and eleven assertions stay green
    while nothing the worker collects moves.
    """
    manual = dump.get("subtitles") or {}
    automatic = dump.get("automatic_captions") or {}

    language = video_language(dump)
    languages = [language] if language is not None else list(fallback_languages)

    candidates: list[CaptionTrack] = []
    for candidate_language in languages:
        for key in _matching_keys(manual, candidate_language):
            track = _track(manual, key, is_automatic=False)
            if track is not None:
                candidates.append(track)
        for key in (
            candidate_language + ORIGINAL_SUFFIX,
            *_matching_keys(automatic, candidate_language),
        ):
            track = _track(automatic, key, is_automatic=True)
            if track is not None:
                candidates.append(track)
    if not candidates:
        raise NotFoundError(_no_track_message(dump, fallback_languages))
    return candidates


def _no_track_message(dump: Mapping[str, Any], fallback_languages: Sequence[str]) -> str:
    """Say which of the three failures this is; they are acted on differently.

    Captions turned off is nothing to do. A video whose own language we cannot
    serve is a fact about that video. Neither is a reason to revisit the
    configured fallback, and a message naming that fallback in all three cases
    invites exactly that — five of forty videos in one sweep read that way.
    """
    if not _caption_languages(dump):
        return "the video has no caption tracks at all"

    language = video_language(dump)
    if language is not None:
        return f"no caption track in the video's own language: {language}"
    return (
        f"the video reports no language and has no caption track in {', '.join(fallback_languages)}"
    )


def parse_json3(
    payload: Mapping[str, Any],
    *,
    language: str = "",
    name: str | None = None,
    is_automatic: bool = False,
) -> Transcript:
    """Turn a json3 caption body into timed segments and one joined string.

    Two shapes in the raw data are easy to get wrong. An event's text can be
    split across several `segs` when part of the line is styled — joining them
    is what keeps a caption line whole instead of shredded. And json3 uses
    text-free events for window positioning, which are furniture rather than
    captions and would otherwise inflate every segment count.
    """
    segments: list[TranscriptSegment] = []
    for event in payload.get("events") or []:
        text = "".join(run.get("utf8", "") for run in event.get("segs") or [])
        if not text.strip():
            continue
        segments.append(
            TranscriptSegment(
                start_seconds=event.get("tStartMs", 0) / 1000,
                duration_seconds=event.get("dDurationMs", 0) / 1000,
                text=text,
            )
        )
    return Transcript(
        language=language,
        name=name,
        is_automatic=is_automatic,
        segments=segments,
        full_text="\n".join(segment.text for segment in segments),
    )


class TranscriptSource:
    """Caption text, including automatically generated tracks.

    Two steps in one job, and they must stay in one job: yt-dlp discovers the
    track URLs, and those URLs are signed and short-lived. Storing one to fetch
    later guarantees a 403 later.
    """

    kind = "video.transcript"
    target_type = TargetType.VIDEO
    lane = Lane.YOUTUBE
    schema_version = "1"
    payload_model: type[BaseModel] = Transcript
    # captions change only on re-upload
    default_freshness = timedelta(days=30)
    cost = SourceCost.STANDARD

    def __init__(self, *, fallback_languages: Sequence[str] = FALLBACK_LANGUAGES) -> None:
        self._fallback_languages = tuple(fallback_languages)

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> Transcript:
        dump = runtime.extract(target, egress=egress)
        candidates = caption_track_candidates(dump, fallback_languages=self._fallback_languages)

        # Walk the ranking rather than committing to its head: a manual track
        # can be refused while the transcription of the same video is served,
        # and returning the video's words in the wrong tier beats failing.
        last_error: UpstreamError | None = None
        for track in candidates:
            try:
                body = egress.fetch(track.url)
            except UpstreamError as error:
                last_error = error
                continue
            return parse_json3(
                json.loads(body),
                language=track.language,
                name=track.name,
                is_automatic=track.is_automatic,
            )

        assert last_error is not None
        raise last_error
