"""The three surfaces only YouTube's own API exposes.

Related videos are absent from yt-dlp's output entirely. Community posts come
back from yt-dlp as a silent empty list. Channel about data — join date,
country, external links — is not there either. All three therefore go direct,
and all three are the most likely thing in this project to break: they read
renderer names YouTube does not version for anyone.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel

from ..egress.control import Lane
from ..egress.transport import Egress
from ..identifiers import TargetType
from ..innertube.client import InnerTubeClient
from ..innertube.parsers import (
    parse_channel_about,
    parse_community_posts,
    parse_related_videos,
)
from ..schemas import ChannelAbout, CommunityPosts, RelatedVideos
from .registry import SourceCost
from .ytdlp_runtime import YtdlpRuntime

# The community tab. Opaque, and hardcoded rather than resolved because
# resolving it needs a second request; if YouTube changes it the parser raises
# on the fallback rather than silently reading the home tab.
COMMUNITY_PARAMS = "Egljb21tdW5pdHnyBgQKAkoA"


class RelatedVideosSource:
    kind = "video.related"
    target_type = TargetType.VIDEO
    lane = Lane.YOUTUBE
    cost = SourceCost.CHEAP
    schema_version = "1"
    payload_model: type[BaseModel] = RelatedVideos
    # Reranked constantly, and cheap to refetch.
    default_freshness = timedelta(hours=1)

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> RelatedVideos:
        response = InnerTubeClient(egress).call("next", {"videoId": target})
        return parse_related_videos(response, video_id=target)


class CommunityPostsSource:
    kind = "channel.community"
    target_type = TargetType.CHANNEL
    lane = Lane.YOUTUBE
    cost = SourceCost.CHEAP
    schema_version = "1"
    payload_model: type[BaseModel] = CommunityPosts
    default_freshness = timedelta(hours=6)

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> CommunityPosts:
        response = InnerTubeClient(egress).call(
            "browse", {"browseId": target, "params": COMMUNITY_PARAMS}
        )
        return parse_community_posts(response, channel_id=target)


class ChannelAboutSource:
    kind = "channel.about"
    target_type = TargetType.CHANNEL
    lane = Lane.YOUTUBE
    cost = SourceCost.CHEAP
    schema_version = "1"
    payload_model: type[BaseModel] = ChannelAbout
    # Join date, country and links effectively never change.
    default_freshness = timedelta(days=7)

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> ChannelAbout:
        response = InnerTubeClient(egress).call("browse", {"browseId": target})
        return parse_channel_about(response, channel_id=target)
