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

# Korean first, English second, and a manual track in either beats both.
DEFAULT_LANGUAGES = ("ko", "en")


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


def _track(bucket: Mapping[str, Any], language: str, *, is_automatic: bool) -> CaptionTrack | None:
    found = _json3_track(bucket.get(language))
    if found is None:
        return None
    return CaptionTrack(
        # `language` rather than the key: an `-orig` key is yt-dlp's marker for
        # the source language, not a language tag anyone should see.
        language=language.removesuffix(ORIGINAL_SUFFIX),
        name=found.get("name"),
        is_automatic=is_automatic,
        format=CAPTION_FORMAT,
        url=found["url"],
    )


def caption_track_candidates(
    dump: Mapping[str, Any], *, languages: Sequence[str]
) -> list[CaptionTrack]:
    """Every track worth trying, best first.

    The requested languages are ranked, and the first one the video has *any*
    track for wins outright. Korean leading the list means a caller gets Korean
    whenever Korean exists — including when it is a machine translation of a
    machine transcription and the uploader's own English captions were sitting
    right there. That is the deliberate trade: whoever asked for Korean wants
    to read Korean, and a faithful transcript in a language they cannot read is
    worth nothing.

    Within one language the ranking is provenance, best first:

    1. A track a person wrote.
    2. The automatic track in the language the transcription actually ran in —
       yt-dlp marks it with an `-orig` key.
    3. The automatic track translated into this language from that one.

    Two and three only differ for a video whose own language is not the one
    being asked for, which is exactly when it matters: `ko` on an English video
    is translated, `ko-orig` on a Korean one is not.

    A list rather than one track because the ranking is a preference and the
    fetch can still be refused — see `TranscriptSource.collect`.
    """
    manual = dump.get("subtitles") or {}
    automatic = dump.get("automatic_captions") or {}

    candidates: list[CaptionTrack] = []
    for language in languages:
        for track in (
            _track(manual, language, is_automatic=False),
            _track(automatic, language + ORIGINAL_SUFFIX, is_automatic=True),
            _track(automatic, language, is_automatic=True),
        ):
            if track is not None:
                candidates.append(track)
    return candidates


def select_caption_track(dump: Mapping[str, Any], *, languages: Sequence[str]) -> CaptionTrack:
    """The single best track. See `caption_track_candidates` for the ranking."""
    candidates = caption_track_candidates(dump, languages=languages)
    if not candidates:
        raise NotFoundError(f"no caption track in any requested language: {', '.join(languages)}")
    return candidates[0]


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

    def __init__(self, *, languages: Sequence[str] = DEFAULT_LANGUAGES) -> None:
        self._languages = tuple(languages)

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> Transcript:
        dump = runtime.extract(target, egress=egress)
        candidates = caption_track_candidates(dump, languages=self._languages)
        if not candidates:
            raise NotFoundError(
                f"no caption track in any requested language: {', '.join(self._languages)}"
            )

        # Walk the ranking rather than committing to its head. YouTube throttles
        # the translation endpoint (`tlang=`) far harder than it throttles the
        # track being translated: measured back to back on one address, the
        # Korean translation of dQw4w9WgXcQ answered 429 every time while the
        # English track it derives from answered 200 every time. Since Korean
        # ranks first, the preferred candidate is the fragile one on exactly
        # those videos, and giving up there would read as "transcripts are
        # broken" rather than "this one track is rationed".
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
