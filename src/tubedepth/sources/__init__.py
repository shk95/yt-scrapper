"""One module per kind of data this project can produce.

The import list below is the extension point. A new data source is a new module
beside these plus its name here — nothing in the worker, the CLI or the API
changes, because dispatch reads the registry rather than a hand-written table.
"""

from __future__ import annotations

from functools import cache

from .registry import DataSource, SourceRegistry
from .transcript import TranscriptSource
from .video_metadata import VideoMetadataSource

__all__ = ["DataSource", "SourceRegistry", "default_registry"]


@cache
def default_registry() -> SourceRegistry:
    registry = SourceRegistry()
    registry.register(VideoMetadataSource())
    registry.register(TranscriptSource())
    return registry
