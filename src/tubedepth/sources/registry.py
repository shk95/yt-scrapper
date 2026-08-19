"""Where the kinds of data this project can produce are registered.

Nothing in here knows anything about YouTube. That is deliberate: the cost of
adding the next data source should be a new module and one registration line,
and it stops being that the moment dispatch grows an if/elif that has to learn
about each new kind.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
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


# How many times a job of each cost is worth trying, applied where the job is
# queued. `Job.max_attempts` said this was done from the day the column was
# written and nothing did it, so every kind took the column default of three:
# a comment harvest is dozens of requests, and three of them against one target
# spend around a hundred requests of the single per-address budget everything
# here competes for — on an address that an UpstreamError suggests is already
# in trouble.
#
# Cheap and standard keep the three they have. Only the expensive case changes,
# because only the expensive case was the argument.
MAXIMUM_ATTEMPTS: Mapping[SourceCost, int] = {
    SourceCost.CHEAP: 3,
    SourceCost.STANDARD: 3,
    SourceCost.EXPENSIVE: 2,
}


def cache_parameters_of(source: DataSource) -> Mapping[str, Any]:
    """What, besides kind and target, makes this source's answer a different answer.

    Read with getattr rather than required on the protocol, for the same reason
    `parts` is: most sources have none, and a required member would make the
    cost of adding a source a line in every source instead of a module and a
    registration.

    An absent declaration is not a special case downstream. `fingerprint()`
    writes `"parameters": {}` into its canonical JSON whether it is handed a
    mapping or nothing at all, so a source that declares nothing keeps exactly
    the fingerprint it has today, byte for byte, and its artifacts stay
    reachable.

    Declare the value, never its relationship to a default. Eliding `limit`
    because it happens to equal DEFAULT_LIMIT would make a listing collected at
    100 key identically to a request for 1,000 the day that constant moves —
    which is the collision this whole change exists to prevent, rebuilt one
    level along.
    """
    return dict(getattr(source, "cache_parameters", None) or {})


def retracted_versions_of(source: DataSource) -> frozenset[str]:
    """Versions of this source whose stored payloads are wrong, not merely old.

    Upgrading is not always the honest move. The only bump this project has
    made, `channel.about` v1 to v2, fixed a parser that read the channel home
    tab as if it were the about panel and returned a video's description as the
    channel's. Lifting that data would launder it; the answer a reader deserves
    is that the observation was withdrawn.

    Read with getattr, like `parts` and `cache_parameters`, so declaring
    nothing is the ordinary case and costs no line in ten sources.
    """
    return frozenset(getattr(source, "retracted_versions", ()) or ())


def attempts_for(source: DataSource) -> int:
    """How many tries this source's jobs get. Read where a job is created."""
    return MAXIMUM_ATTEMPTS[source.cost]


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
    # come out of the same per-address Google tolerance; SponsorBlock has its
    # own. The distinction is the point: a source put on the wrong lane spends
    # a budget it was chosen to avoid.
    lane: Lane
    cost: SourceCost
    # Bumped when this source's normalization changes shape. It is part of the
    # cache fingerprint, so changing a normalizer invalidates its own cached
    # answers rather than leaving them fresh-looking and a version behind.
    schema_version: str
    # The model `collect` returns, so a cached payload can be parsed back from
    # disk — a cache that cannot reproduce the parsed value forces every
    # consumer of one to refetch. Implementations must annotate this as
    # `type[BaseModel]` rather than letting it infer: a mutable protocol member
    # is invariant, so an inferred `type[VideoMetadata]` does not satisfy it.
    payload_model: type[BaseModel]
    # How long an answer stays good. A property of the data: captions barely
    # change, view counts change constantly.
    default_freshness: timedelta

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
                "freshness_seconds": int(self.get(kind).default_freshness.total_seconds()),
                # The effective values, not the built-in ones. `serve` and
                # `work` are separate processes reading the same environment
                # variables once each, so this is how an operator compares
                # what the two of them actually believe — a disagreement makes
                # the API answer for a question the worker did not collect.
                "cache_parameters": cache_parameters_of(self.get(kind)),
            }
            for kind in self.kinds()
        }
