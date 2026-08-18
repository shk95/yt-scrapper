"""Subtitles and transcripts, including automatically generated ones.

The official Data API lists caption tracks but will not hand over their text
without the video owner's OAuth, so for anyone else this is the only route.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..egress.transport import Egress
from ..errors import NotFoundError
from ..identifiers import TargetType
from ..schemas import Transcript, TranscriptSegment
from .ytdlp_runtime import YtdlpRuntime

# json3 specifically: it is the only format YouTube offers that carries the
# per-segment timings as data rather than as text needing a second parser.
CAPTION_FORMAT = "json3"


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


def select_caption_track(dump: Mapping[str, Any], *, language: str) -> CaptionTrack:
    """Pick the track to fetch for `language`.

    A manual track wins over an automatic one for the same language: one is
    written by a person and the other is a transcription, and they will
    disagree. Callers who want the machine transcription can ask for it once
    that is a thing anyone has asked for.
    """
    candidates = (
        (dump.get("subtitles") or {}, False),
        (dump.get("automatic_captions") or {}, True),
    )
    for bucket, is_automatic in candidates:
        track = _json3_track(bucket.get(language))
        if track is None:
            continue
        return CaptionTrack(
            language=language,
            name=track.get("name"),
            is_automatic=is_automatic,
            format=CAPTION_FORMAT,
            url=track["url"],
        )
    raise NotFoundError(f"no caption track for language: {language}")


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

    def __init__(self, *, language: str = "en") -> None:
        self._language = language

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> Transcript:
        dump = runtime.extract(target, egress=egress)
        track = select_caption_track(dump, language=self._language)
        body = egress.fetch(track.url)
        return parse_json3(
            json.loads(body),
            language=track.language,
            name=track.name,
            is_automatic=track.is_automatic,
        )
