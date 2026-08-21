"""The flatten pass, against a real PostgreSQL.

These are integration tests on purpose. Everything interesting about
`FlattenService` is what the database does with what it sends: the cursor's
tuple comparison, `ON CONFLICT` with a `WHERE` that refuses to regress a
comment, one transaction per batch. A fake session would assert the
statements were built, which is the half that was never in doubt.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from tubedepth import flatten as flatten_module
from tubedepth.database import Database
from tubedepth.flatten import FlattenService
from tubedepth.models import (
    Artifact,
    ChannelSnapshot,
    CommentRecord,
    FlattenProgress,
    ListingEntry,
    TranscriptRecord,
    VideoSnapshot,
)
from tubedepth.payload_store import PayloadStore

COLLECTED = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
# Far enough after every artifact below that the safety lag never holds one
# back — the one test that is about the lag moves its own clock instead.
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class Recorder:
    """Payloads on disk and their artifact rows in the index, as collection leaves them.

    Identifiers are handed out in insertion order and sort that way, so a
    test that wants two observations at the same instant still gets the
    order it wrote them in.
    """

    def __init__(self, database: Database, payloads: PayloadStore) -> None:
        self._database = database
        self._payloads = payloads
        self._recorded = 0

    def store(
        self,
        kind: str,
        target: str,
        payload: dict[str, object],
        *,
        fetched_at: datetime = COLLECTED,
    ) -> str:
        return self.store_bytes(kind, target, json.dumps(payload).encode(), fetched_at=fetched_at)

    def store_bytes(
        self, kind: str, target: str, raw: bytes, *, fetched_at: datetime = COLLECTED
    ) -> str:
        stored = self._payloads.put(kind, raw)
        return self.record(kind, target, stored.digest, fetched_at=fetched_at)

    def record(
        self, kind: str, target: str, digest: str, *, fetched_at: datetime = COLLECTED
    ) -> str:
        identifier = f"artifact{self._recorded:04d}"
        self._recorded += 1
        with self._database.session() as session:
            session.add(
                Artifact(
                    identifier=identifier,
                    kind=kind,
                    target=target,
                    fingerprint=identifier,
                    digest=digest,
                    byte_count=1,
                    fetched_at=fetched_at,
                    fresh_until=fetched_at + timedelta(hours=6),
                )
            )
        return identifier


@pytest.fixture
def payloads(tmp_path) -> PayloadStore:
    return PayloadStore(tmp_path / "payloads")


@pytest.fixture
def recorder(database: Database, payloads: PayloadStore) -> Recorder:
    return Recorder(database, payloads)


def service(database: Database, payloads: PayloadStore, clock: FakeClock) -> FlattenService:
    return FlattenService(database=database, payloads=payloads, clock=clock)


def rows(database: Database, model: type) -> list:
    with database.session(readonly=True) as session:
        return list(session.scalars(select(model)))


def count(database: Database, model: type) -> int:
    with database.session(readonly=True) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def forget_the_cursor(database: Database) -> None:
    """Reset progress so the next run re-reads everything.

    The replay tests need the same artifacts flattened twice, which is what
    a re-run after an operator clears the cursor actually does.
    """
    with database.session() as session:
        for progress in session.scalars(select(FlattenProgress)):
            session.delete(progress)


def metadata(video_id: str, title: str, views: int) -> dict[str, object]:
    return {
        "video_id": video_id,
        "title": title,
        "channel": "A Channel",
        "channel_id": "UC123",
        "duration_seconds": 212,
        "view_count": views,
        "like_count": 10,
        "comment_count": 3,
        "published_at": "2026-08-01T00:00:00+00:00",
    }


def comments(*items: tuple[str, str, int]) -> dict[str, object]:
    return {
        "comments": [
            {
                "comment_id": comment_id,
                "text": text,
                "author": "Someone",
                "author_id": "UCauthor",
                "like_count": likes,
                "published_at": "2026-08-02T00:00:00+00:00",
            }
            for comment_id, text, likes in items
        ]
    }


def test_a_metadata_artifact_becomes_one_snapshot_row(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    artifact_id = recorder.store(
        "video.metadata", "dQw4w9WgXcQ", metadata("dQw4w9WgXcQ", "Never Gonna", 1000)
    )

    outcome = service(database, payloads, FakeClock()).run()

    assert outcome.artifacts_seen == 1
    assert outcome.flattened == {"video.metadata": 1}
    assert outcome.errors == 0
    assert outcome.cursor_fetched_at == COLLECTED

    snapshots = rows(database, VideoSnapshot)
    assert len(snapshots) == 1
    assert snapshots[0].artifact_id == artifact_id
    assert snapshots[0].title == "Never Gonna"
    assert snapshots[0].view_count == 1000
    assert snapshots[0].fetched_at == COLLECTED


def test_running_twice_adds_no_rows(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    recorder.store("video.metadata", "vid1", metadata("vid1", "One", 1))
    flatten = service(database, payloads, FakeClock())

    first = flatten.run()
    # Nothing new to read: the cursor already sits past the only artifact.
    assert flatten.run().artifacts_seen == 0

    # And re-reading the same artifact from a cleared cursor is a no-op on
    # the rows too, which is the half `ON CONFLICT DO NOTHING` is for.
    forget_the_cursor(database)
    second = flatten.run()

    assert second.artifacts_seen == first.artifacts_seen == 1
    assert second.flattened == first.flattened
    assert count(database, VideoSnapshot) == 1


def test_a_listing_fans_out_to_entry_rows(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    recorder.store(
        "search.videos",
        "화장품",
        {
            "videos": [
                {"video_id": "first", "title": "First", "view_count": 5},
                {"title": "a private entry with no id"},
                {"video_id": "third", "title": "Third", "view_count": 7},
            ]
        },
    )

    outcome = service(database, payloads, FakeClock()).run()

    assert outcome.flattened == {"search.videos": 1}
    entries = sorted(rows(database, ListingEntry), key=lambda entry: entry.position)
    assert [(entry.position, entry.video_id) for entry in entries] == [(0, "first"), (2, "third")]
    assert {entry.target for entry in entries} == {"화장품"}
    assert {entry.kind for entry in entries} == {"search.videos"}


def test_a_bundle_routes_its_parts(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    artifact_id = recorder.store(
        "video.bundle",
        "vid1",
        {
            "parts": {
                "video.metadata": metadata("vid1", "Bundled", 42),
                "video.comments": comments(("c1", "nice", 3), ("c2", "also nice", 1)),
            }
        },
    )

    outcome = service(database, payloads, FakeClock()).run()

    assert outcome.flattened == {"video.bundle": 1}
    assert outcome.skipped_unhandled == 0

    snapshots = rows(database, VideoSnapshot)
    assert [(row.artifact_id, row.title) for row in snapshots] == [(artifact_id, "Bundled")]
    assert {row.comment_id for row in rows(database, CommentRecord)} == {"c1", "c2"}
    assert {row.video_id for row in rows(database, CommentRecord)} == {"vid1"}


def test_an_unhandled_bundle_part_is_counted(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    recorder.store(
        "video.bundle",
        "vid1",
        {"parts": {"video.metadata": metadata("vid1", "Bundled", 1), "video.related": {}}},
    )

    outcome = service(database, payloads, FakeClock()).run()

    assert outcome.flattened == {"video.bundle": 1}
    assert outcome.skipped_unhandled == 1
    assert count(database, VideoSnapshot) == 1


def test_a_channel_about_artifact_becomes_a_channel_snapshot(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    recorder.store(
        "channel.about",
        "UC123",
        {
            "channel_id": "UC123",
            "name": "A Channel",
            "handle": "@achannel",
            "subscriber_count_approximate": 12300,
            "video_count": 45,
        },
    )

    outcome = service(database, payloads, FakeClock()).run()

    assert outcome.flattened == {"channel.about": 1}
    snapshots = rows(database, ChannelSnapshot)
    assert len(snapshots) == 1
    assert snapshots[0].handle == "@achannel"
    assert snapshots[0].subscriber_count_approximate == 12300


def test_comments_deduplicate_across_harvests(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    later = COLLECTED + timedelta(days=1)
    recorder.store("video.comments", "vid1", comments(("c1", "first wording", 3)))
    recorder.store(
        "video.comments",
        "vid1",
        comments(("c1", "edited wording", 9), ("c2", "a new one", 0)),
        fetched_at=later,
    )

    outcome = service(database, payloads, FakeClock()).run()

    assert outcome.artifacts_seen == 2
    assert outcome.flattened == {"video.comments": 2}

    stored = {row.comment_id: row for row in rows(database, CommentRecord)}
    assert set(stored) == {"c1", "c2"}
    assert stored["c1"].text == "edited wording"
    assert stored["c1"].like_count == 9
    # The lifespan is what the two harvests together observed.
    assert stored["c1"].first_seen_at == COLLECTED
    assert stored["c1"].last_seen_at == later
    assert stored["c2"].first_seen_at == later


def test_an_older_replay_does_not_regress_a_comment(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    later = COLLECTED + timedelta(days=1)
    recorder.store("video.comments", "vid1", comments(("c1", "first wording", 3)))
    recorder.store(
        "video.comments", "vid1", comments(("c1", "edited wording", 9)), fetched_at=later
    )
    flatten = service(database, payloads, FakeClock())
    flatten.run()

    forget_the_cursor(database)
    flatten.run()

    stored = rows(database, CommentRecord)
    assert len(stored) == 1
    assert stored[0].text == "edited wording"
    assert stored[0].like_count == 9
    assert stored[0].first_seen_at == COLLECTED
    assert stored[0].last_seen_at == later


def test_transcripts_keep_the_newest(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    later = COLLECTED + timedelta(days=1)
    recorder.store(
        "video.transcript",
        "vid1",
        {"language": "en", "full_text": "old words", "segments": [1, 2]},
    )
    recorder.store(
        "video.transcript",
        "vid1",
        {"language": "en", "full_text": "new words", "segments": [1, 2, 3]},
        fetched_at=later,
    )
    flatten = service(database, payloads, FakeClock())
    flatten.run()

    stored = rows(database, TranscriptRecord)
    assert len(stored) == 1
    assert stored[0].full_text == "new words"
    assert stored[0].segment_count == 3
    assert stored[0].fetched_at == later

    # A replay of the older observation must not put the old words back.
    forget_the_cursor(database)
    flatten.run()
    replayed = rows(database, TranscriptRecord)
    assert len(replayed) == 1
    assert replayed[0].full_text == "new words"


def test_a_missing_payload_is_skipped_and_the_cursor_passes_it(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    # Retention deletes payloads; an artifact row outliving its blob is the
    # ordinary end of that, not a fault.
    recorder.record("video.metadata", "gone", "0" * 64)
    recorder.store(
        "video.metadata", "vid1", metadata("vid1", "Still here", 1), fetched_at=COLLECTED
    )
    flatten = service(database, payloads, FakeClock())

    outcome = flatten.run()

    assert outcome.artifacts_seen == 2
    assert outcome.skipped_missing_payload == 1
    assert outcome.errors == 0
    assert count(database, VideoSnapshot) == 1
    # The cursor moved past the missing one rather than stalling on it.
    assert flatten.run().artifacts_seen == 0


def test_an_unreadable_payload_counts_as_an_error_not_a_crash(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    recorder.store_bytes("video.metadata", "vid1", b"not json at all")
    # A payload that decodes but has no usable title: a FlattenError, counted
    # the same way.
    recorder.store("video.metadata", "vid2", {"video_id": "vid2"})
    recorder.store("video.metadata", "vid3", metadata("vid3", "Fine", 1))
    flatten = service(database, payloads, FakeClock())

    outcome = flatten.run()

    assert outcome.artifacts_seen == 3
    assert outcome.errors == 2
    assert outcome.flattened == {"video.metadata": 1}
    assert count(database, VideoSnapshot) == 1
    assert flatten.run().artifacts_seen == 0


def test_a_row_the_database_refuses_is_an_error_the_walk_passes(
    database: Database, payloads: PayloadStore, recorder: Recorder, monkeypatch
) -> None:
    """One unwritable row must not stall the pipeline for ever.

    The transforms refuse the oversize fields they can see, so this poisons a
    handler instead: it is the only way to reach the case they cannot see —
    anything the database refuses for a reason flatten did not anticipate.
    Without the per-artifact savepoint that row aborts the batch, the cursor
    never passes it, and every firing after it dies on the same artifact.
    """
    poison = "v" * (flatten_module.IDENTIFIER_LIMIT + 1)
    original = flatten_module._HANDLERS["video.metadata"]

    def poisoned(observation, payload):
        rows = original.transform(observation, payload)
        if payload.get("title") == "Poisoned":
            rows[0]["video_id"] = poison
        return rows

    monkeypatch.setitem(
        flatten_module._HANDLERS,
        "video.metadata",
        flatten_module._Handler(poisoned, original.upsert),
    )

    recorder.store("video.metadata", "vid1", metadata("vid1", "Fine", 1))
    recorder.store(
        "video.metadata",
        "vid2",
        metadata("vid2", "Poisoned", 2),
        fetched_at=COLLECTED + timedelta(minutes=1),
    )
    recorder.store(
        "video.metadata",
        "vid3",
        metadata("vid3", "Also fine", 3),
        fetched_at=COLLECTED + timedelta(minutes=2),
    )
    flatten = service(database, payloads, FakeClock())

    outcome = flatten.run()

    assert outcome.artifacts_seen == 3
    assert outcome.errors == 1
    assert outcome.flattened == {"video.metadata": 2}
    # The two good artifacts in the same batch still committed.
    assert sorted(row.title for row in rows(database, VideoSnapshot)) == ["Also fine", "Fine"]
    # And the cursor is past the bad one, so the next firing is not the same
    # failure again.
    assert flatten.run().artifacts_seen == 0


def test_an_unhandled_kind_is_counted(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    recorder.store("video.related", "vid1", {"videos": []})
    flatten = service(database, payloads, FakeClock())

    outcome = flatten.run()

    assert outcome.artifacts_seen == 1
    assert outcome.skipped_unhandled == 1
    assert outcome.flattened == {}
    assert count(database, ListingEntry) == 0
    assert flatten.run().artifacts_seen == 0


def test_the_cursor_resumes_where_it_stopped(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    recorder.store("video.metadata", "vid1", metadata("vid1", "First", 1))
    recorder.store(
        "video.metadata",
        "vid2",
        metadata("vid2", "Second", 2),
        fetched_at=COLLECTED + timedelta(hours=1),
    )
    flatten = service(database, payloads, FakeClock())

    first = flatten.run(limit=1)
    assert first.artifacts_seen == 1
    assert [row.title for row in rows(database, VideoSnapshot)] == ["First"]

    second = flatten.run()
    assert second.artifacts_seen == 1
    assert second.cursor_fetched_at == COLLECTED + timedelta(hours=1)
    assert sorted(row.title for row in rows(database, VideoSnapshot)) == ["First", "Second"]


def test_the_safety_lag_holds_back_fresh_artifacts(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    clock = FakeClock()
    recorder.store(
        "video.metadata",
        "vid1",
        metadata("vid1", "Fresh", 1),
        fetched_at=NOW - timedelta(minutes=1),
    )
    flatten = service(database, payloads, clock)

    assert flatten.run().artifacts_seen == 0
    assert count(database, VideoSnapshot) == 0

    clock.advance(timedelta(minutes=10))

    assert flatten.run().artifacts_seen == 1
    assert count(database, VideoSnapshot) == 1


def test_dry_run_writes_nothing(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    recorder.store("video.metadata", "vid1", metadata("vid1", "One", 1))
    recorder.store("video.comments", "vid1", comments(("c1", "hello", 0)))
    recorder.store("video.related", "vid1", {})
    flatten = service(database, payloads, FakeClock())

    rehearsed = flatten.run(dry_run=True)

    assert rehearsed.artifacts_seen == 3
    assert rehearsed.flattened == {"video.metadata": 1, "video.comments": 1}
    assert rehearsed.skipped_unhandled == 1
    assert count(database, VideoSnapshot) == 0
    assert count(database, CommentRecord) == 0
    assert count(database, FlattenProgress) == 0

    # And the real run that follows reports exactly what the rehearsal did.
    real = flatten.run()
    assert real.artifacts_seen == rehearsed.artifacts_seen
    assert real.flattened == rehearsed.flattened
    assert real.skipped_unhandled == rehearsed.skipped_unhandled
    assert count(database, VideoSnapshot) == 1
    assert count(database, FlattenProgress) == 1


def test_a_batch_smaller_than_the_backlog_still_reaches_the_end(
    database: Database, payloads: PayloadStore, recorder: Recorder
) -> None:
    for index in range(5):
        recorder.store(
            "video.metadata",
            f"vid{index}",
            metadata(f"vid{index}", f"Video {index}", index),
            fetched_at=COLLECTED + timedelta(minutes=index),
        )

    outcome = service(database, payloads, FakeClock()).run(batch_size=2)

    assert outcome.artifacts_seen == 5
    assert count(database, VideoSnapshot) == 5
