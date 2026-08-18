"""Not fetching what we already have.

Throughput against YouTube is capped by YouTube, so the only large multiplier
left is not asking twice. A channel swept weekly is mostly unchanged, and a
re-collection that refetches all of it spends the whole budget learning that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tubedepth.database import Database
from tubedepth.repositories import ArtifactRepository

START = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = START) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def prepared(tmp_path: Path) -> Database:
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    return database


def test_a_recorded_artifact_is_found_again_by_its_fingerprint(tmp_path: Path) -> None:
    clock = FakeClock()
    database = prepared(tmp_path)

    with database.session() as session:
        ArtifactRepository(session, clock=clock).record(
            kind="video.metadata",
            target="dQw4w9WgXcQ",
            fingerprint="abc123",
            digest="deadbeef",
            byte_count=1234,
            freshness=timedelta(hours=6),
        )

    with database.session() as session:
        found = ArtifactRepository(session, clock=clock).fresh("abc123")
        assert found is not None
        assert found.digest == "deadbeef"
        assert found.byte_count == 1234


def test_an_artifact_past_its_freshness_is_not_served(tmp_path: Path) -> None:
    clock = FakeClock()
    database = prepared(tmp_path)
    with database.session() as session:
        ArtifactRepository(session, clock=clock).record(
            kind="video.metadata",
            target="dQw4w9WgXcQ",
            fingerprint="abc123",
            digest="deadbeef",
            byte_count=1,
            freshness=timedelta(hours=6),
        )

    clock.advance(timedelta(hours=6, seconds=1))

    with database.session() as session:
        assert ArtifactRepository(session, clock=clock).fresh("abc123") is None


def test_a_different_question_does_not_hit_the_cache(tmp_path: Path) -> None:
    clock = FakeClock()
    database = prepared(tmp_path)
    with database.session() as session:
        ArtifactRepository(session, clock=clock).record(
            kind="video.metadata",
            target="dQw4w9WgXcQ",
            fingerprint="abc123",
            digest="deadbeef",
            byte_count=1,
            freshness=timedelta(hours=6),
        )

    with database.session() as session:
        assert ArtifactRepository(session, clock=clock).fresh("different") is None


def test_recollecting_records_a_new_row_rather_than_overwriting(tmp_path: Path) -> None:
    """History is a by-product worth keeping.

    View, like and comment counts move, and keeping each observation makes how
    they moved a free consequence of caching rather than a separate feature.
    The cost is disk, which retention bounds.
    """
    clock = FakeClock()
    database = prepared(tmp_path)
    for digest in ("first", "second"):
        with database.session() as session:
            ArtifactRepository(session, clock=clock).record(
                kind="video.metadata",
                target="dQw4w9WgXcQ",
                fingerprint="abc123",
                digest=digest,
                byte_count=1,
                freshness=timedelta(hours=6),
            )
        clock.advance(timedelta(hours=7))

    with database.session() as session:
        assert ArtifactRepository(session, clock=clock).count_for("abc123") == 2

    # Asked at a moment inside the second observation's window and outside the
    # first's, the newer one is what comes back.
    inside_the_second = FakeClock(START + timedelta(hours=8))
    with database.session() as session:
        served = ArtifactRepository(session, clock=inside_the_second).fresh("abc123")
        assert served is not None
        assert served.digest == "second"
