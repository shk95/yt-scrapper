"""Several things about one video, in one request.

The value is not saving three round trips; it is that partial failure gets a
name. A video with captions turned off still has metadata, sponsor segments and
related videos, and a client asking for "everything about this video" should
get those with the gap recorded rather than an error for the lot.

This source is the one composite in the project, and it does not collect
anything itself. It declares which kinds it is made of and the collection
service fans out through itself — which is what makes the parts cacheable. A
bundle asked for seconds after a metadata collect must not fetch the metadata
again, and calling the sources directly would.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel

from ..egress.control import Lane
from ..egress.transport import Egress
from ..errors import TubedepthError
from ..identifiers import TargetType
from ..schemas import VideoBundle
from .registry import SourceCost
from .ytdlp_runtime import YtdlpRuntime

# What "everything about this video" means by default. Comments are absent on
# purpose: a harvest runs for minutes and costs a hundred requests, so folding
# it in would turn a quick composite into the most expensive job in the system
# and every bundle would inherit that.
DEFAULT_PARTS = (
    "video.metadata",
    "video.transcript",
    "video.sponsor_segments",
    "video.related",
)


class BundleSource:
    kind = "video.bundle"
    target_type = TargetType.VIDEO
    # Nominal. The service never calls `collect` here; each part is dispatched
    # on its own lane and against its own budget.
    lane = Lane.YOUTUBE
    # Expensive because it is several jobs wearing one name: it must not take a
    # slot reserved for the sub-second work it would otherwise starve.
    cost = SourceCost.EXPENSIVE
    schema_version = "1"
    payload_model: type[BaseModel] = VideoBundle
    # The shortest freshness among the parts would be more correct and is not
    # worth the coupling: a bundle is a convenience view, and each part's own
    # artifact keeps its own freshness for anyone who cares.
    default_freshness = timedelta(hours=6)

    def __init__(self, *, parts: tuple[str, ...] = DEFAULT_PARTS) -> None:
        self.parts = parts

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> VideoBundle:
        """Never called. A composite is assembled by the collection service.

        Present so the source satisfies the same protocol as every other one —
        the registry, the worker and `/v1/sources` need no special case — and
        raising rather than returning something empty means a dispatcher that
        forgot the composite path fails loudly instead of storing a husk.
        """
        raise TubedepthError(
            f"{self.kind} is a composite and is assembled by the collection service"
        )
