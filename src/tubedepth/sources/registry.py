"""Where the kinds of data this project can produce are registered.

Nothing in here knows anything about YouTube. That is deliberate: the cost of
adding the next data source should be a new module and one registration line,
and it stops being that the moment dispatch grows an if/elif that has to learn
about each new kind.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from ..egress.control import Lane
from ..egress.transport import Egress
from ..errors import ConfigurationError, NotFoundError
from ..identifiers import TargetType
from .ytdlp_runtime import YtdlpRuntime


class SourceCost(StrEnum):
    """How long a job of this kind runs, which is what starvation depends on.

    Not a guess: a comment harvest is roughly fifty requests and minutes of
    wall clock, against three requests and two seconds for metadata. Slots are
    reserved per cost so the slow ones cannot occupy every worker.
    """

    CHEAP = "cheap"  # sub-second, one request
    STANDARD = "standard"  # seconds, a handful of requests
    EXPENSIVE = "expensive"  # minutes, dozens of requests


@runtime_checkable
class DataSource(Protocol):
    """One kind of data, fetched and normalized.

    The backend is deliberately absent from this protocol. video.metadata
    drives yt-dlp, video.transcript drives yt-dlp and then a plain HTTP GET,
    and a future source may drive InnerTube — and the job machinery cannot
    tell them apart. That is what makes adding a backend cheap.
    """

    kind: str
    # Which normalizer applies to this source's target. A property of the
    # source rather than of the caller: a channel handle run through the video
    # normalizer is rejected as malformed, and a video id run through the
    # channel one is accepted and then fails inside the extractor minutes later.
    target_type: TargetType
    # Which budget this draws on. yt-dlp, InnerTube and caption fetches all
    # come out of the same per-address Google tolerance; Return YouTube Dislike
    # and SponsorBlock each have their own.
    lane: Lane
    cost: SourceCost

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> BaseModel: ...


class SourceRegistry:
    def __init__(self) -> None:
        self._sources: dict[str, DataSource] = {}

    def register(self, source: DataSource) -> DataSource:
        if source.kind in self._sources:
            raise ConfigurationError(f"source kind is already registered: {source.kind}")
        self._sources[source.kind] = source
        return source

    def get(self, kind: str) -> DataSource:
        try:
            return self._sources[kind]
        except KeyError:
            raise NotFoundError(f"no source registered for kind: {kind}") from None

    def kinds(self) -> list[str]:
        return sorted(self._sources)

    def describe(self) -> Mapping[str, Any]:
        """What a `GET /v1/sources` route and the CLI help both read."""
        return {
            kind: {
                "kind": kind,
                "target": self.get(kind).target_type.value,
                "lane": self.get(kind).lane.value,
                "cost": self.get(kind).cost.value,
            }
            for kind in self.kinds()
        }
