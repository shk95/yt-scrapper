"""The registry that makes adding a data source cheap.

The whole extensibility claim rests here: a new kind of data should be one new
module plus one registration, with nothing in the worker, the CLI or the API
changing. These tests are what stop that claim quietly becoming false.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import BaseModel

from tubedepth.egress.control import Lane
from tubedepth.egress.transport import Egress
from tubedepth.errors import ConfigurationError, NotFoundError
from tubedepth.identifiers import TargetType
from tubedepth.sources.registry import SourceCost, SourceRegistry
from tubedepth.sources.ytdlp_runtime import YtdlpRuntime


class FakePayload(BaseModel):
    target: str


class FakeSource:
    """A source that actually satisfies the protocol.

    Typed rather than left loose on purpose: a fake that does not satisfy the
    contract is a fake that lies, and the first thing it hides is a change to
    the contract itself.
    """

    kind = "video.fake"
    target_type = TargetType.VIDEO
    lane = Lane.YOUTUBE
    cost = SourceCost.STANDARD
    schema_version = "1"
    payload_model: type[BaseModel] = FakePayload
    default_freshness = timedelta(hours=6)

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> FakePayload:
        return FakePayload(target=target)


def test_a_registered_source_can_be_looked_up_by_its_kind() -> None:
    registry = SourceRegistry()
    source = FakeSource()

    registry.register(source)

    assert registry.get("video.fake") is source


def test_registering_two_sources_for_one_kind_is_rejected() -> None:
    # A silent overwrite here is a merge accident that loses one of them with
    # no trace, and the loser is whichever module happened to import second.
    registry = SourceRegistry()
    registry.register(FakeSource())

    with pytest.raises(ConfigurationError, match="already registered"):
        registry.register(FakeSource())


def test_asking_for_an_unregistered_kind_is_a_domain_error() -> None:
    with pytest.raises(NotFoundError, match="no source registered"):
        SourceRegistry().get("video.nonexistent")


def test_the_registry_reports_what_it_holds() -> None:
    # This is what a `GET /v1/sources` route and `tubedepth collect --help`
    # both read, so a new source documents itself for free.
    registry = SourceRegistry()
    registry.register(FakeSource())

    assert registry.kinds() == ["video.fake"]


def test_every_source_the_project_ships_declares_a_kind() -> None:
    # A meta-test over the real registry: adding a source should be safe rather
    # than careful, and forgetting the one required attribute should be caught
    # here rather than by an AttributeError inside a worker.
    from tubedepth.sources import default_registry

    registry = default_registry()

    assert registry.kinds(), "the project ships no sources at all"
    for kind in registry.kinds():
        assert registry.get(kind).kind == kind


def test_every_shipped_source_declares_a_lane_and_a_cost() -> None:
    """Both are what the scheduler reads, and neither has a safe default.

    A missing lane would put a SponsorBlock lookup on YouTube's budget, which
    is the one budget this project actually runs out of; a missing cost would
    let a comment harvest take a slot reserved for the sub-second jobs it would
    otherwise starve.
    """
    from tubedepth.egress.control import Lane
    from tubedepth.sources import default_registry
    from tubedepth.sources.registry import SourceCost

    registry = default_registry()

    for kind in registry.kinds():
        source = registry.get(kind)
        assert isinstance(source.lane, Lane), kind
        assert isinstance(source.cost, SourceCost), kind


def test_a_comment_harvest_is_declared_more_expensive_than_a_metadata_fetch() -> None:
    # Not decoration: this ordering is what the reserved slots are built on,
    # and it is measured — a harvest is ~50 requests against metadata's ~3.
    from tubedepth.sources import default_registry
    from tubedepth.sources.registry import SourceCost

    registry = default_registry()

    assert registry.get("video.comments").cost is SourceCost.EXPENSIVE
    assert registry.get("video.metadata").cost is SourceCost.STANDARD
