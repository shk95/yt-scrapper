"""Where the kinds of data this project can produce are registered.

Nothing in here knows anything about YouTube. That is deliberate: the cost of
adding the next data source should be a new module and one registration line,
and it stops being that the moment dispatch grows an if/elif that has to learn
about each new kind.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from ..egress.transport import Egress
from ..errors import ConfigurationError, NotFoundError
from .ytdlp_runtime import YtdlpRuntime


@runtime_checkable
class DataSource(Protocol):
    """One kind of data, fetched and normalized.

    The backend is deliberately absent from this protocol. video.metadata
    drives yt-dlp, video.transcript drives yt-dlp and then a plain HTTP GET,
    and a future source may drive InnerTube — and the job machinery cannot
    tell them apart. That is what makes adding a backend cheap.
    """

    kind: str

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
        return {kind: {"kind": kind} for kind in self.kinds()}
