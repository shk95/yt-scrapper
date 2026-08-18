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
from ..errors import NotFoundError
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


def select_caption_track(dump: Mapping[str, Any], *, languages: Sequence[str]) -> CaptionTrack:
    """Pick the track to fetch, best first.

    Three tiers, and the order is the whole content of this function:

    1. A track a person wrote, in the first preferred language that has one.
       This wins across languages, not just within one: an English transcript
       written by the uploader is better than a Korean machine translation of
       a machine transcription, even for a caller who asked for Korean first.
    2. The automatic track in the video's *own* language, if that language was
       asked for. yt-dlp marks it with an `-orig` key; every other automatic
       language is that transcription run through a translator, so preferring
       it by language order alone would quietly pick the two-step-lossy one.
    3. Any automatic track, in preferred order.
    """
    manual = dump.get("subtitles") or {}
    automatic = dump.get("automatic_captions") or {}

    for language in languages:
        track = _track(manual, language, is_automatic=False)
        if track is not None:
            return track

    for language in languages:
        track = _track(automatic, language + ORIGINAL_SUFFIX, is_automatic=True)
        if track is not None:
            return track

    for language in languages:
        track = _track(automatic, language, is_automatic=True)
        if track is not None:
            return track

    raise NotFoundError(f"no caption track in any requested language: {', '.join(languages)}")


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
        track = select_caption_track(dump, languages=self._languages)
        body = egress.fetch(track.url)
        return parse_json3(
            json.loads(body),
            language=track.language,
            name=track.name,
            is_automatic=track.is_automatic,
        )
