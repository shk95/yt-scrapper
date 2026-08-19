"""Comment harvests.

The Data API exposes comments but spends quota fast enough that collecting
them at any volume is impractical; this route has no quota and its cost is
time instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from ..egress.control import Lane
from ..egress.transport import Egress
from ..identifiers import TargetType
from ..schemas import Comment, CommentHarvest
from .registry import SourceCost
from .ytdlp_runtime import YtdlpRuntime

DEFAULT_LIMIT = 200
ROOT_SENTINEL = "root"


def _published_at(timestamp: int | None) -> datetime | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC)


def _comment(raw: Mapping[str, Any]) -> Comment:
    parent = raw.get("parent")
    return Comment(
        comment_id=raw["id"],
        parent_id=None if parent in (None, ROOT_SENTINEL) else parent,
        text=raw.get("text", ""),
        like_count=raw.get("like_count"),
        author=raw.get("author"),
        author_id=raw.get("author_id"),
        author_is_uploader=bool(raw.get("author_is_uploader")),
        author_is_verified=bool(raw.get("author_is_verified")),
        is_pinned=bool(raw.get("is_pinned")),
        is_hearted_by_uploader=bool(raw.get("is_favorited")),
        published_at=_published_at(raw.get("timestamp")),
        published_text=raw.get("_time_text"),
    )


def normalize(
    dump: Mapping[str, Any],
    *,
    sort: str,
    requested_limit: int | None = None,
) -> CommentHarvest:
    comments = [_comment(raw) for raw in dump.get("comments") or []]
    return CommentHarvest(
        sort=sort,
        retrieved_count=len(comments),
        is_truncated=requested_limit is not None and len(comments) >= requested_limit,
        comments=comments,
    )


class CommentsSource:
    """Every comment we are willing to wait for, threaded by parent.

    The expensive one: roughly one request per twenty comments, so a
    thousand-comment video is fifty-odd requests and minutes of wall clock —
    all of it drawn from the same per-address budget the metadata sources need.
    `limit` is the real control, not an optimisation.
    """

    kind = "video.comments"
    target_type = TargetType.VIDEO
    lane = Lane.YOUTUBE
    schema_version = "1"
    payload_model: type[BaseModel] = CommentHarvest
    # the most expensive thing here; a day-old harvest is still useful
    default_freshness = timedelta(hours=24)
    cost = SourceCost.EXPENSIVE

    def __init__(self, *, sort: str = "top", limit: int = DEFAULT_LIMIT) -> None:
        self._sort = sort
        self._limit = limit

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> CommentHarvest:
        dump = runtime.extract(
            target,
            egress=egress,
            options={
                "getcomments": True,
                "extractor_args": {
                    "youtube": {
                        "comment_sort": [self._sort],
                        "max_comments": [str(self._limit), "all", "all", "8"],
                    }
                },
            },
        )
        return normalize(dump, sort=self._sort, requested_limit=self._limit)
