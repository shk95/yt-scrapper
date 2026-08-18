"""Collecting one kind of data and putting it where it can be read again."""

from __future__ import annotations

from .identifiers import normalize_video_identifier
from .payload_store import PayloadStore, StoredPayload
from .sources.video_metadata import normalize
from .sources.ytdlp_runtime import YtdlpRuntime


class CollectionService:
    def __init__(self, *, runtime: YtdlpRuntime, payloads: PayloadStore) -> None:
        self._runtime = runtime
        self._payloads = payloads

    def collect_video_metadata(self, target: str) -> StoredPayload:
        video_id = normalize_video_identifier(target)
        dump = self._runtime.extract(video_id)
        metadata = normalize(dump)
        return self._payloads.put(
            "video.metadata",
            metadata.model_dump_json(indent=1).encode(),
        )
