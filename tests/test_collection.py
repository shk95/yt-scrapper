"""Collecting a video's metadata end to end, with the network faked out.

The only thing standing between this and a real collection is which
YtdlpRuntime is injected — which is the point of the seam.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from tubedepth.collection import CollectionService
from tubedepth.egress.control import Lane
from tubedepth.egress.transport import Egress
from tubedepth.identifiers import TargetType
from tubedepth.payload_store import PayloadStore
from tubedepth.sources import SourceRegistry
from tubedepth.sources.registry import SourceCost
from tubedepth.sources.ytdlp_runtime import YtdlpRuntime

FIXTURES = Path(__file__).parent / "fixtures/ytdlp/video_metadata"


class RecordedYtdlpRuntime:
    """Replays a recorded dump instead of reaching YouTube.

    Every other test in this suite would have to be marked `live` without it,
    which means they would not run in CI at all.
    """

    def __init__(self, dump: Mapping[str, Any]) -> None:
        self._dump = dump
        self.requested: list[str] = []
        self.options: list[dict[str, Any]] = []

    def extract(
        self,
        target: str,
        *,
        egress: Egress,
        options: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        self.requested.append(target)
        self.options.append(dict(options or {}))
        return self._dump


@pytest.fixture
def runtime() -> RecordedYtdlpRuntime:
    with gzip.open(FIXTURES / "2026-08-18-dQw4w9WgXcQ.json.gz", "rt", encoding="utf-8") as handle:
        return RecordedYtdlpRuntime(json.load(handle))


def test_collecting_a_video_stores_its_normalized_metadata(
    tmp_path: Path, runtime: RecordedYtdlpRuntime
) -> None:
    service = CollectionService(runtime=runtime, payloads=PayloadStore(tmp_path))

    collected = service.collect("video.metadata", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    payload = json.loads(PayloadStore(tmp_path).read(collected.payload.digest))
    assert payload["video_id"] == "dQw4w9WgXcQ"
    assert len(payload["tags"]) == 27
    assert len(payload["most_replayed"]) == 100


def test_collection_hands_the_runtime_a_normalized_identifier(
    tmp_path: Path, runtime: RecordedYtdlpRuntime
) -> None:
    # A caller pasting a share link and a caller pasting a bare id must reach
    # the same extraction, or the store fills with duplicates of one video.
    service = CollectionService(runtime=runtime, payloads=PayloadStore(tmp_path))

    service.collect("video.metadata", "https://youtu.be/dQw4w9WgXcQ")

    assert runtime.requested == ["dQw4w9WgXcQ"]


def test_an_unknown_kind_is_refused_before_anything_is_extracted(
    tmp_path: Path, runtime: RecordedYtdlpRuntime
) -> None:
    # The registry is consulted first, so a typo in a kind costs nothing and
    # never reaches YouTube.
    from tubedepth.errors import NotFoundError

    service = CollectionService(runtime=runtime, payloads=PayloadStore(tmp_path))

    with pytest.raises(NotFoundError, match="no source registered"):
        service.collect("video.nonexistent", "dQw4w9WgXcQ")

    assert runtime.requested == []


class ChannelListingSource:
    """A source whose target is a channel, not a video."""

    kind = "channel.fake"
    target_type = TargetType.CHANNEL
    lane = Lane.YOUTUBE
    cost = SourceCost.STANDARD

    def __init__(self) -> None:
        self.received: list[str] = []

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> FakeListing:
        self.received.append(target)
        return FakeListing(target=target)


class FakeListing(BaseModel):
    target: str


def test_a_channel_target_is_normalized_as_a_channel_and_not_as_a_video(
    tmp_path: Path, runtime: RecordedYtdlpRuntime
) -> None:
    # Which normalizer applies is a property of the source. Running every
    # target through the video one rejects a channel handle as a malformed
    # video id, before the source that understands it is ever consulted.
    source = ChannelListingSource()
    registry = SourceRegistry()
    registry.register(source)
    service = CollectionService(runtime=runtime, payloads=PayloadStore(tmp_path), registry=registry)

    service.collect("channel.fake", "https://www.youtube.com/@RickAstleyYT")

    assert source.received == ["@RickAstleyYT"]
