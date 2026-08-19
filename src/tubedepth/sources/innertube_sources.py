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
from ..errors import ExtractionError
from ..identifiers import TargetType
from ..innertube.client import InnerTubeCaller, InnerTubeClient
from ..innertube.parsers import (
    parse_channel_about,
    parse_community_posts,
    parse_related_videos,
)
from ..innertube.renderers import find_all, observed_renderers
from ..schemas import ChannelAbout, CommunityPosts, RelatedVideos
from .registry import SourceCost
from .ytdlp_runtime import YtdlpRuntime

# The community tab. Opaque, and hardcoded rather than resolved because
# resolving it needs a second request; if YouTube changes it the parser raises
# on the fallback rather than silently reading the home tab.
COMMUNITY_PARAMS = "Egljb21tdW5pdHnyBgQKAkoA"


def browse_id_for(client: InnerTubeCaller, target: str) -> str:
    """A `browseId` for a channel, resolving a handle if that is what we have.

    The identifier layer accepts `@handle` because YouTube URLs use them, but
    `browse` only takes a `UC...` id and answers 400 for anything else — an
    error about a request the caller never made. One extra round trip, and
    only when the target is a handle.
    """
    if not target.startswith("@"):
        return target
    response = client.call("navigation/resolve_url", {"url": f"https://www.youtube.com/{target}"})
    browse_id = next(
        (
            endpoint["browseId"]
            for endpoint in find_all(response, "browseEndpoint")
            if endpoint.get("browseId")
        ),
        None,
    )
    if browse_id is None:
        raise ExtractionError(f"could not resolve a channel id for handle: {target}")
    return browse_id


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
        client = InnerTubeClient(egress)
        channel_id = browse_id_for(client, target)
        response = client.call("browse", {"browseId": channel_id, "params": COMMUNITY_PARAMS})
        return parse_community_posts(response, channel_id=channel_id)


class ChannelAboutSource:
    kind = "channel.about"
    target_type = TargetType.CHANNEL
    lane = Lane.YOUTUBE
    cost = SourceCost.CHEAP
    # 2: was the channel home tab read as if it were the about panel, which
    # returned a video's description as the channel's and nothing else at all.
    schema_version = "2"
    payload_model: type[BaseModel] = ChannelAbout
    # Join date, country and links effectively never change.
    default_freshness = timedelta(days=7)

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> ChannelAbout:
        """Two calls, because YouTube stopped having an About tab.

        The plan assumed about-tab `params` that would go stale; what actually
        happened is that the surface moved. There is no About tab in the tab
        list at all — the data lives behind a continuation from an engagement
        panel — so the token is read from the first response at runtime rather
        than hardcoded. A hardcoded token would be a credential-shaped string
        that expires, which is the failure this source already had once.
        """
        client = InnerTubeClient(egress)
        channel_id = browse_id_for(client, target)
        home = client.call("browse", {"browseId": channel_id})
        token = next(
            (
                candidate["token"]
                for candidate in find_all(home, "continuationCommand")
                if candidate.get("token")
            ),
            None,
        )
        if token is None:
            observed = ", ".join(sorted(observed_renderers(home))[:12])
            raise ExtractionError(
                f"no continuation to the about panel for channel {target}; observed: {observed}"
            )
        return parse_channel_about(
            client.call("browse", {"continuation": token}),
            channel_id=channel_id,
            metadata=home,
        )
