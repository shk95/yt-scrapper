"""Keeping the store bounded, and saying so when it is not.

Two mechanisms, on purpose. Pruning by age is the normal path and is what
keeps usage proportional to what is actually current. The size ceiling is a
backstop: reaching it means the age policy is not doing its job, so it is
reported rather than quietly absorbed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tubedepth.database import Database
from tubedepth.models import Artifact
from tubedepth.payload_store import PayloadStore
from tubedepth.retention import RetentionPolicy, RetentionService

START = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = START) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def build(
    tmp_path: Path, policy: RetentionPolicy, clock: FakeClock
) -> tuple[Database, PayloadStore, RetentionService]:
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    payloads = PayloadStore(tmp_path / "payloads")
    service = RetentionService(database=database, payloads=payloads, policy=policy, clock=clock)
    return database, payloads, service


def store(
    database: Database, payloads: PayloadStore, clock: FakeClock, body: bytes, target: str
) -> str:
    stored = payloads.put("video.metadata", body)
    with database.session() as session:
        session.add(
            Artifact(
                kind="video.metadata",
                target=target,
                fingerprint=f"fp-{target}",
                digest=stored.digest,
                byte_count=stored.byte_count,
                fetched_at=clock(),
                fresh_until=clock() + timedelta(hours=6),
            )
        )
    return stored.digest


def test_an_artifact_past_its_retention_age_is_removed(tmp_path: Path) -> None:
    clock = FakeClock()
    database, payloads, service = build(
        tmp_path, RetentionPolicy(maximum_age=timedelta(days=30)), clock
    )
    digest = store(database, payloads, clock, b'{"a": 1}', "old")

    clock.advance(timedelta(days=31))
    outcome = service.prune()

    assert outcome.artifacts_removed == 1
    assert payloads.path_for("video.metadata", digest) is None
    with database.session() as session:
        assert session.query(Artifact).count() == 0


def test_a_recent_artifact_is_kept(tmp_path: Path) -> None:
    clock = FakeClock()
    database, payloads, service = build(
        tmp_path, RetentionPolicy(maximum_age=timedelta(days=30)), clock
    )
    store(database, payloads, clock, b'{"a": 1}', "recent")

    clock.advance(timedelta(days=1))
    outcome = service.prune()

    assert outcome.artifacts_removed == 0
    with database.session() as session:
        assert session.query(Artifact).count() == 1


def test_age_is_the_only_thing_that_protects_an_artifact(tmp_path: Path) -> None:
    """Nothing survives on the grounds of being the last of its kind.

    An earlier design kept the newest observation of each question regardless
    of age, so that a stale answer would beat none. It would not: `fresh()`
    filters on fresh_until, so a month-old artifact is never served. Protecting
    it bought no cache hits and cost unbounded growth, because the store would
    grow with the number of distinct things ever collected rather than with
    what is current.
    """
    clock = FakeClock()
    database, payloads, service = build(
        tmp_path, RetentionPolicy(maximum_age=timedelta(days=30)), clock
    )
    store(database, payloads, clock, b'{"v": 1}', "only-one")
    clock.advance(timedelta(days=40))

    outcome = service.prune()

    assert outcome.artifacts_removed == 1
    with database.session() as session:
        assert session.query(Artifact).count() == 0


def test_exceeding_the_size_ceiling_is_reported_rather_than_absorbed(
    tmp_path: Path,
) -> None:
    # The ceiling is a backstop, not an operating point. Reaching it means the
    # age policy is not keeping up, and silently evicting hides that.
    clock = FakeClock()
    database, payloads, service = build(
        tmp_path,
        RetentionPolicy(maximum_age=timedelta(days=30), maximum_bytes=100),
        clock,
    )
    for index in range(3):
        store(database, payloads, clock, b"x" * 60, f"video{index:06d}")

    outcome = service.prune()

    assert outcome.over_ceiling is True
    assert outcome.total_bytes > 100


def test_staying_under_the_ceiling_is_not_reported_as_a_problem(tmp_path: Path) -> None:
    clock = FakeClock()
    database, payloads, service = build(
        tmp_path,
        RetentionPolicy(maximum_age=timedelta(days=30), maximum_bytes=10_000),
        clock,
    )
    store(database, payloads, clock, b"x" * 60, "small")

    outcome = service.prune()

    assert outcome.over_ceiling is False
