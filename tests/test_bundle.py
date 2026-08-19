"""One request that collects several things about a video.

The value is not saving three HTTP calls; it is that partial failure gets a
name. A video with captions turned off should still yield its metadata, its
segments and its related videos, with the missing part recorded as a
degradation rather than failing the whole request or silently disappearing.

Fanning out *through the collection service* rather than calling sources
directly is what makes the parts cacheable: a bundle asked for right after a
metadata collect must not fetch the metadata again.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from tubedepth.collection import CollectionService
from tubedepth.database import Database
from tubedepth.egress.control import Lane
from tubedepth.egress.transport import Egress
from tubedepth.errors import NotFoundError
from tubedepth.identifiers import TargetType
from tubedepth.payload_store import PayloadStore
from tubedepth.schemas import VideoBundle
from tubedepth.sources import SourceRegistry
from tubedepth.sources.bundle import BundleSource
from tubedepth.sources.registry import SourceCost
from tubedepth.sources.ytdlp_runtime import YtdlpRuntime


class Payload(BaseModel):
    value: str


def part(name: str, *, fails: bool = False) -> Any:
    class Part:
        kind = name
        target_type = TargetType.VIDEO
        lane = Lane.YOUTUBE
        cost = SourceCost.CHEAP
        schema_version = "1"
        payload_model: type[BaseModel] = Payload
        default_freshness = timedelta(hours=6)

        def __init__(self) -> None:
            self.calls = 0

        def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> Payload:
            self.calls += 1
            if fails:
                raise NotFoundError(f"{name} has nothing for {target}")
            return Payload(value=f"{name}:{target}")

    return Part()


def service(tmp_path: Path, *parts: Any, database: Database | None = None) -> CollectionService:
    registry = SourceRegistry()
    for source in parts:
        registry.register(source)
    registry.register(BundleSource(parts=tuple(source.kind for source in parts)))
    return CollectionService(
        payloads=PayloadStore(tmp_path / "payloads"), database=database, registry=registry
    )


def bundle_of(collected: object) -> VideoBundle:
    """`Collected.result` is typed as the protocol's BaseModel, so the concrete
    shape has to be asserted rather than assumed."""
    result = getattr(collected, "result", None)
    assert isinstance(result, VideoBundle)
    return result


def test_a_bundle_collects_every_part(tmp_path: Path) -> None:
    first, second = part("video.first"), part("video.second")

    collected = service(tmp_path, first, second).collect("video.bundle", "dQw4w9WgXcQ")

    result = bundle_of(collected)
    assert set(result.parts) == {"video.first", "video.second"}
    assert result.degradations == []


def test_a_failing_part_becomes_a_degradation_and_not_a_failed_bundle(tmp_path: Path) -> None:
    """The whole point. Losing one part of six is still a useful answer."""
    good, bad = part("video.first"), part("video.second", fails=True)

    collected = service(tmp_path, good, bad).collect("video.bundle", "dQw4w9WgXcQ")

    result = bundle_of(collected)
    assert set(result.parts) == {"video.first"}
    assert len(result.degradations) == 1
    degradation = result.degradations[0]
    assert degradation.source == "video.second"
    assert degradation.code == "NotFoundError"
    assert "nothing for" in degradation.detail


def test_a_bundle_whose_every_part_fails_is_a_failure(tmp_path: Path) -> None:
    """A bundle of nothing is not a result. Returning an empty success would
    make "collected" and "collected nothing" the same answer."""
    with pytest.raises(NotFoundError):
        service(tmp_path, part("video.first", fails=True)).collect("video.bundle", "dQw4w9WgXcQ")


def test_a_bundle_reuses_what_was_already_collected(tmp_path: Path) -> None:
    """Fanning out through the collection service is what buys this.

    Calling the sources directly would refetch a metadata payload collected
    seconds earlier, which on the one budget that caps this system is the
    expensive kind of convenience.
    """
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    first = part("video.first")
    collector = service(tmp_path, first, database=database)
    collector.collect("video.first", "dQw4w9WgXcQ")
    assert first.calls == 1

    collector.collect("video.bundle", "dQw4w9WgXcQ")

    assert first.calls == 1, "the bundle refetched a part it already had"


def test_the_bundle_declares_itself_like_any_other_source(tmp_path: Path) -> None:
    """It appears in `/v1/sources` and the worker dispatches it unchanged."""
    bundle = BundleSource(parts=("video.first",))

    assert bundle.kind == "video.bundle"
    assert bundle.target_type is TargetType.VIDEO
    assert bundle.cost is SourceCost.EXPENSIVE
