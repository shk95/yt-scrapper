"""One module per kind of data this project can produce.

The import list below is the extension point. A new data source is a new module
beside these plus its name here — nothing in the worker, the CLI or the API
changes, because dispatch reads the registry rather than a hand-written table.
"""

from __future__ import annotations

import os
from functools import cache

from ..errors import ConfigurationError
from .bundle import BundleSource
from .comments import DEFAULT_LIMIT as COMMENTS_DEFAULT_LIMIT
from .comments import CommentsSource
from .innertube_sources import (
    ChannelAboutSource,
    CommunityPostsSource,
    RelatedVideosSource,
)
from .listings import DEFAULT_LIMIT as LISTINGS_DEFAULT_LIMIT
from .listings import ChannelVideosSource, PlaylistItemsSource, SearchVideosSource
from .registry import DataSource, SourceRegistry
from .sponsorblock import SponsorBlockSource
from .transcript import TranscriptSource
from .trending import DEFAULT_LIMIT as TRENDING_DEFAULT_LIMIT
from .trending import TrendingVideosSource
from .video_metadata import VideoMetadataSource

__all__ = ["DataSource", "SourceRegistry", "default_registry"]

# The built-in caps, and the reason they are caps rather than targets: a bigger
# listing is one extraction to yt-dlp but many continuation requests underneath,
# out of the same per-address budget everything else draws on.
LISTING_LIMIT = LISTINGS_DEFAULT_LIMIT
COMMENT_LIMIT = COMMENTS_DEFAULT_LIMIT
# The chart is the one cap that costs Google quota rather than the per-address
# budget: one request per fifty results, so 200 is four units and 50 is one.
TRENDING_LIMIT = TRENDING_DEFAULT_LIMIT


def _limit(variable: str, fallback: int) -> int:
    """A deployment-wide cap, or the built-in one.

    Refused rather than defaulted when it does not parse. An operator who set
    it and silently got the old behaviour concludes the variable does nothing —
    and the sweep they ran is exactly the size they were trying to change.
    """
    raw = os.environ.get(variable)
    if raw is None:
        return fallback
    if not raw.isdigit() or int(raw) < 1:
        raise ConfigurationError(f"{variable} is not a positive whole number: {raw!r}")
    return int(raw)


@cache
def default_registry() -> SourceRegistry:
    """The kinds this build can collect.

    Cached, so the caps below are read once per process — and `tubedepth serve`
    and `tubedepth work` are separate processes. **Set them identically in both
    units.** If they disagree, the API computes a different cache key than the
    worker records, so it stops matching anything the worker writes and keeps
    matching rows written before the change: a 100-item listing served for a
    request the worker would have collected at 1,000. `GET /v1/sources` reports
    the effective values, which is how the two are compared.
    """
    listing = _limit("TUBEDEPTH_LISTING_LIMIT", LISTING_LIMIT)
    comments = _limit("TUBEDEPTH_COMMENT_LIMIT", COMMENT_LIMIT)
    trending = _limit("TUBEDEPTH_TRENDING_LIMIT", TRENDING_LIMIT)

    registry = SourceRegistry()
    registry.register(VideoMetadataSource())
    registry.register(TranscriptSource())
    # Last, so every kind it fans out to is already registered.
    registry.register(BundleSource())
    registry.register(CommentsSource(limit=comments))
    registry.register(ChannelVideosSource(limit=listing))
    registry.register(SearchVideosSource(limit=listing))
    registry.register(PlaylistItemsSource(limit=listing))
    registry.register(SponsorBlockSource())
    registry.register(RelatedVideosSource())
    registry.register(CommunityPostsSource())
    registry.register(ChannelAboutSource())
    registry.register(TrendingVideosSource(limit=trending))
    return registry
