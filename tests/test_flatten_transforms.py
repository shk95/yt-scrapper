"""The payload-to-row transforms, as pure functions.

Deliberately no database and no Pydantic models here: flatten reads stored
JSON of any historical schema_version, so the transforms are exercised on
plain dicts — including dicts missing fields today's models would require.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tubedepth.flatten import (
    FlattenError,
    Observation,
    channel_snapshot_row,
    comment_rows,
    listing_entry_rows,
    transcript_row,
    video_snapshot_row,
)

FETCHED = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def observed(kind: str, target: str) -> Observation:
    return Observation(artifact_id="a" * 32, kind=kind, target=target, fetched_at=FETCHED)


class TestVideoSnapshot:
    def test_flattens_the_counts_and_identity(self) -> None:
        row = video_snapshot_row(
            observed("video.metadata", "abc123"),
            {
                "video_id": "abc123",
                "title": "A title",
                "channel": "Chan",
                "channel_id": "UC1",
                "duration_seconds": 61,
                "view_count": 1000,
                "like_count": 10,
                "comment_count": 5,
                "published_at": "2026-08-01T00:00:00+00:00",
                "published_date": "2026-08-01",
            },
        )
        assert row["artifact_id"] == "a" * 32
        assert row["video_id"] == "abc123"
        assert row["fetched_at"] == FETCHED
        assert row["view_count"] == 1000
        assert row["published_at"] == datetime(2026, 8, 1, tzinfo=UTC)
        assert row["published_date"].isoformat() == "2026-08-01"

    def test_missing_optionals_become_none(self) -> None:
        row = video_snapshot_row(
            observed("video.metadata", "abc123"),
            {"video_id": "abc123", "title": "A title"},
        )
        assert row["view_count"] is None
        assert row["published_at"] is None
        assert row["published_date"] is None

    def test_a_payload_without_a_video_id_is_refused(self) -> None:
        with pytest.raises(FlattenError):
            video_snapshot_row(observed("video.metadata", "abc123"), {"title": "A title"})

    def test_a_naive_instant_is_read_as_utc(self) -> None:
        row = video_snapshot_row(
            observed("video.metadata", "abc123"),
            {"video_id": "abc123", "title": "t", "published_at": "2026-08-01T00:00:00"},
        )
        assert row["published_at"] == datetime(2026, 8, 1, tzinfo=UTC)


class TestListingEntries:
    def test_positions_follow_list_order(self) -> None:
        rows = listing_entry_rows(
            observed("search.videos", "화장품"),
            {
                "videos": [
                    {"video_id": "v1", "title": "one", "view_count": 5},
                    {"video_id": "v2"},
                ]
            },
        )
        assert [(r["position"], r["video_id"]) for r in rows] == [(0, "v1"), (1, "v2")]
        assert rows[0]["kind"] == "search.videos"
        assert rows[0]["target"] == "화장품"
        assert rows[1]["title"] is None

    def test_an_entry_without_a_video_id_is_dropped_not_fatal(self) -> None:
        rows = listing_entry_rows(
            observed("search.videos", "q"),
            {"videos": [{"title": "placeholder"}, {"video_id": "v2"}]},
        )
        assert [r["video_id"] for r in rows] == ["v2"]
        # Positions still reflect the listing as observed, so v2 stays at 1.
        assert rows[0]["position"] == 1

    def test_an_empty_listing_is_a_real_answer(self) -> None:
        assert listing_entry_rows(observed("search.videos", "q"), {"videos": []}) == []


class TestChannelSnapshot:
    def test_flattens_the_about_panel(self) -> None:
        row = channel_snapshot_row(
            observed("channel.about", "UC1"),
            {
                "channel_id": "UC1",
                "name": "Chan",
                "handle": "@chan",
                "subscriber_count_approximate": 4530000,
                "view_count": 999,
                "video_count": 12,
                "country": "KR",
            },
        )
        assert row["channel_id"] == "UC1"
        assert row["subscriber_count_approximate"] == 4530000

    def test_a_payload_without_a_channel_id_is_refused(self) -> None:
        with pytest.raises(FlattenError):
            channel_snapshot_row(observed("channel.about", "UC1"), {"name": "Chan"})


class TestCommentRows:
    def test_video_id_comes_from_the_target(self) -> None:
        rows = comment_rows(
            observed("video.comments", "abc123"),
            {
                "comments": [
                    {
                        "comment_id": "c1",
                        "text": "hi",
                        "like_count": 2,
                        "is_pinned": True,
                        "published_at": "2026-08-20T00:00:00+00:00",
                    }
                ]
            },
        )
        (row,) = rows
        assert row["video_id"] == "abc123"
        assert row["comment_id"] == "c1"
        assert row["first_seen_at"] == FETCHED
        assert row["last_seen_at"] == FETCHED
        assert row["is_pinned"] is True
        assert row["is_hearted_by_uploader"] is False

    def test_a_comment_without_an_id_is_dropped(self) -> None:
        rows = comment_rows(
            observed("video.comments", "abc123"),
            {"comments": [{"text": "no id"}, {"comment_id": "c2", "text": "ok"}]},
        )
        assert [r["comment_id"] for r in rows] == ["c2"]

    def test_duplicate_ids_within_one_harvest_keep_the_last(self) -> None:
        rows = comment_rows(
            observed("video.comments", "abc123"),
            {
                "comments": [
                    {"comment_id": "c1", "text": "first"},
                    {"comment_id": "c1", "text": "second"},
                ]
            },
        )
        (row,) = rows
        assert row["text"] == "second"


class TestTranscriptRow:
    def test_flattens_the_transcript(self) -> None:
        row = transcript_row(
            observed("video.transcript", "abc123"),
            {"language": "ko", "is_automatic": True, "full_text": "말", "segments": [1, 2]},
        )
        assert row == {
            "video_id": "abc123",
            "language": "ko",
            "is_automatic": True,
            "full_text": "말",
            "segment_count": 2,
            "fetched_at": FETCHED,
        }

    def test_a_transcript_without_a_language_is_refused(self) -> None:
        with pytest.raises(FlattenError):
            transcript_row(observed("video.transcript", "abc123"), {"full_text": "말"})
