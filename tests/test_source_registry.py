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


def test_every_declared_cache_parameter_survives_the_canonical_json() -> None:
    """The cache key must not depend on a repr an object is free to change.

    `fingerprint()` falls back to `default=str` for anything it cannot
    serialise, which turns a `timedelta` or a tuple of objects into a key that
    moves when that type's `__str__` does — a silent mass invalidation with no
    change anyone made.
    """
    from tubedepth.sources import default_registry
    from tubedepth.sources.registry import cache_parameters_of

    simple = (str, int, float, bool, type(None))
    offenders: list[str] = []
    for kind in default_registry().kinds():
        for name, value in cache_parameters_of(default_registry().get(kind)).items():
            values = value if isinstance(value, list) else [value]
            if not all(isinstance(entry, simple) for entry in values):
                offenders.append(f"{kind}.{name}={value!r}")

    assert not offenders, f"cache parameters that do not round-trip as JSON: {offenders}"


def test_the_listing_cap_can_be_raised_for_a_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A channel with more than a hundred videos could not be collected in full.

    The constant was a constructor default frozen at registration, so the only
    way to sweep a thousand-video channel was to edit the source.
    """
    from tubedepth.sources import default_registry
    from tubedepth.sources.registry import cache_parameters_of

    monkeypatch.setenv("TUBEDEPTH_LISTING_LIMIT", "1000")
    default_registry.cache_clear()

    try:
        assert cache_parameters_of(default_registry().get("channel.videos")) == {"limit": 1000}
    finally:
        default_registry.cache_clear()


def test_raising_the_cap_asks_a_different_question_than_the_old_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half of issue #2 that must not be skipped, closed end to end.

    Raising the constant alone was a silent wrong answer: the limit was not in
    the cache key, so re-running a channel swept an hour ago served the cached
    100-item listing for the request that asked for 1,000. Nothing errored and
    the sweep was quietly missing 900 videos.
    """
    from tubedepth.fingerprints import fingerprint
    from tubedepth.sources import default_registry
    from tubedepth.sources.registry import cache_parameters_of

    def question(limit: str) -> str:
        monkeypatch.setenv("TUBEDEPTH_LISTING_LIMIT", limit)
        default_registry.cache_clear()
        source = default_registry().get("channel.videos")
        return fingerprint(
            kind=source.kind,
            target="@someone",
            schema_version=source.schema_version,
            parameters=cache_parameters_of(source),
        )

    try:
        assert question("100") != question("1000")
    finally:
        default_registry.cache_clear()


def test_a_cap_that_is_not_a_number_is_refused_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falling back to the default would be worse than refusing.

    An operator who set it and got the old behaviour anyway concludes the
    variable does nothing — and the sweep they ran is silently the size they
    were trying to change.
    """
    from tubedepth.errors import ConfigurationError
    from tubedepth.sources import default_registry

    monkeypatch.setenv("TUBEDEPTH_LISTING_LIMIT", "lots")
    default_registry.cache_clear()

    try:
        with pytest.raises(ConfigurationError, match="TUBEDEPTH_LISTING_LIMIT"):
            default_registry()
    finally:
        default_registry.cache_clear()


def test_the_source_listing_reports_the_caps_actually_in_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`serve` and `work` read the environment once each, in different processes.

    If they disagree the API computes a different cache key than the worker
    records, so it stops matching what the worker writes while still matching
    rows from before the change. Comparing the two instances is the only way to
    see that, and it needs both of them to say what they believe.
    """
    from tubedepth.sources import default_registry

    monkeypatch.setenv("TUBEDEPTH_LISTING_LIMIT", "750")
    default_registry.cache_clear()

    try:
        described = default_registry().describe()
    finally:
        default_registry.cache_clear()

    assert described["channel.videos"]["cache_parameters"] == {"limit": 750}
    assert described["video.metadata"]["cache_parameters"] == {}


def test_the_trending_chart_size_is_configurable_like_every_other_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TrendingVideosSource` took a `limit` and declared it in its cache key,
    and nothing could set it.

    So the parameter separated a top-50 chart from a top-200 one while only one
    of those could ever be asked for — a knob with no caller, in a source that
    also happens to be the only one spending Google quota rather than the
    per-address budget, where four requests versus one is the whole difference.
    """
    from tubedepth.sources import default_registry
    from tubedepth.sources.registry import cache_parameters_of

    monkeypatch.setenv("TUBEDEPTH_TRENDING_LIMIT", "50")
    default_registry.cache_clear()

    try:
        assert cache_parameters_of(default_registry().get("trending.videos")) == {"limit": 50}
    finally:
        default_registry.cache_clear()
