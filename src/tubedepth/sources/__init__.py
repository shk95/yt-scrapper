"""One module per kind of data this project can produce.

The import list below is the extension point. A new data source is a new module
beside these plus its name here — nothing in the worker, the CLI or the API
changes, because dispatch reads the registry rather than a hand-written table.
"""

from __future__ import annotations

from functools import cache

from .bundle import BundleSource
from .comments import CommentsSource
from .innertube_sources import (
    ChannelAboutSource,
    CommunityPostsSource,
    RelatedVideosSource,
)
from .listings import ChannelVideosSource, PlaylistItemsSource, SearchVideosSource
from .registry import DataSource, SourceRegistry
from .sponsorblock import SponsorBlockSource
from .transcript import TranscriptSource
from .trending import TrendingVideosSource
from .video_metadata import VideoMetadataSource

__all__ = ["DataSource", "SourceRegistry", "default_registry"]


@cache
def default_registry() -> SourceRegistry:
    registry = SourceRegistry()
    registry.register(VideoMetadataSource())
    registry.register(TranscriptSource())
    # Last, so every kind it fans out to is already registered.
    registry.register(BundleSource())
    registry.register(CommentsSource())
    registry.register(ChannelVideosSource())
    registry.register(SearchVideosSource())
    registry.register(PlaylistItemsSource())
    registry.register(SponsorBlockSource())
    registry.register(RelatedVideosSource())
    registry.register(CommunityPostsSource())
    registry.register(ChannelAboutSource())
    registry.register(TrendingVideosSource())
    return registry
