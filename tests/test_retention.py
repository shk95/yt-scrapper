"""Keeping the store bounded, and saying so when it is not.

Two mechanisms, on purpose. Pruning by age is the normal path and is what
keeps usage proportional to what is actually current. The size ceiling is a
backstop: reaching it means the age policy is not doing its job, so it is
reported rather than quietly absorbed.
"""

from __future__ import annotations

import json
import random
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
    #
    # The payloads are distinct and incompressible on purpose. Sixty identical
    # `x` bytes three times over is one file after content addressing and about
    # thirty bytes after gzip, so the arithmetic only worked while the ceiling
    # was measured against uncompressed row totals.
    clock = FakeClock()
    database, payloads, service = build(
        tmp_path,
        RetentionPolicy(maximum_age=timedelta(days=30), maximum_bytes=100),
        clock,
    )
    generator = random.Random(20260819)
    for index in range(3):
        body = bytes(generator.randrange(256) for _ in range(200))
        store(database, payloads, clock, body, f"video{index:06d}")

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


def test_a_blob_with_no_artifact_row_is_swept(tmp_path: Path) -> None:
    """Orphans are produced routinely, not exceptionally.

    `tubedepth collect` writes a payload and no row — it takes no database at
    all — so every CLI collection leaves one. Ten were sitting in the working
    store when this was written, and nothing in the system could ever have
    removed them: `prune` walks artifact rows and deletes *their* payloads, so
    a file without a row is unreachable by construction.
    """
    clock = FakeClock(datetime(2026, 8, 19, tzinfo=UTC))
    database, payloads, service = build(tmp_path, RetentionPolicy(), clock)
    stored = payloads.put("video.metadata", b'{"orphan": true}')
    clock.advance(timedelta(days=1))
    assert payloads.path_for("video.metadata", stored.digest) is not None

    outcome = service.prune()

    assert payloads.path_for("video.metadata", stored.digest) is None
    assert outcome.orphans_removed == 1


def test_a_blob_written_moments_ago_is_left_alone(tmp_path: Path) -> None:
    """The race this sweep must not lose.

    Payloads are written before their artifact row — deliberately, so a crash
    leaves an orphan rather than a row pointing at nothing. That means every
    successful collection is briefly an orphan, and a sweep with no grace
    period would delete the result of a job that is still committing.
    """
    clock = FakeClock(datetime(2026, 8, 19, tzinfo=UTC))
    database, payloads, service = build(tmp_path, RetentionPolicy(), clock)
    stored = payloads.put("video.metadata", b'{"just": "written"}')

    outcome = service.prune()

    assert payloads.path_for("video.metadata", stored.digest) is not None
    assert outcome.orphans_removed == 0


def test_the_store_size_is_measured_on_disk_and_not_from_the_rows(tmp_path: Path) -> None:
    """The ceiling's only job is to describe the disk, so it must measure it.

    `byte_count` is the uncompressed payload size. Reporting that as the store
    size overstated the working store by five times — 4.5 MiB claimed against
    0.91 MiB of files — so a 50 GiB ceiling would have fired at roughly 10 GiB
    of actual use. Gzip is the whole reason the blob store exists; a size that
    ignores it is not a size.
    """
    clock = FakeClock(datetime(2026, 8, 19, tzinfo=UTC))
    database, payloads, service = build(tmp_path, RetentionPolicy(), clock)
    body = json.dumps({"text": "compressible " * 500}).encode()
    digest = store(database, payloads, clock, body, "video000001")
    stored_path = payloads.path_for("video.metadata", digest)

    outcome = service.prune()

    assert stored_path is not None
    assert outcome.total_bytes == stored_path.stat().st_size
    assert outcome.total_bytes < len(body) / 2, "gzip is why the blob store exists"


def test_an_expiring_observation_does_not_take_a_payload_a_current_one_shares(
    tmp_path: Path,
) -> None:
    """Two identical observations are one blob, and the older one expires first.

    Content addressing makes this the ordinary case rather than a corner:
    `docs/api.md` tells readers that equal digests across two `fetched_at`
    values mean nothing changed, so a video whose counts have not moved has
    exactly this shape. Unlinking on the expiring row leaves the surviving row
    pointing at nothing — a cache entry that can never be served and a job
    result that raises instead of answering.
    """
    clock = FakeClock()
    database, payloads, service = build(
        tmp_path, RetentionPolicy(maximum_age=timedelta(days=30)), clock
    )
    unchanged = b'{"view_count": 100}'
    digest = store(database, payloads, clock, unchanged, "same-video")
    clock.advance(timedelta(days=29))
    assert store(database, payloads, clock, unchanged, "same-video") == digest

    clock.advance(timedelta(days=2))
    outcome = service.prune()

    assert outcome.artifacts_removed == 1
    with database.session() as session:
        assert session.query(Artifact).count() == 1
    assert payloads.read(digest) == unchanged, (
        "the surviving observation's payload was unlinked along with the expiring one"
    )
