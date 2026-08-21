"""Keeping the store bounded, and saying so when it is not.

Two mechanisms, on purpose. Pruning by age is the normal path and is what
keeps usage proportional to what is actually current. The size ceiling is a
backstop: reaching it means the age policy is not doing its job, so it is
reported rather than quietly absorbed.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from tubedepth.database import Database
from tubedepth.errors import ConfigurationError
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
    tmp_path: Path, database: Database, policy: RetentionPolicy, clock: FakeClock
) -> tuple[Database, PayloadStore, RetentionService]:
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


def _age(path: Path, by: timedelta) -> None:
    """Backdate a file, because the orphan sweep reads its mtime and not a clock."""
    when = path.stat().st_mtime - by.total_seconds()
    os.utime(path, (when, when))


def test_an_artifact_past_its_retention_age_is_removed(tmp_path: Path, database: Database) -> None:
    clock = FakeClock()
    database, payloads, service = build(
        tmp_path, database, RetentionPolicy(maximum_age=timedelta(days=30)), clock
    )
    digest = store(database, payloads, clock, b'{"a": 1}', "old")

    clock.advance(timedelta(days=31))
    outcome = service.prune()

    assert outcome.artifacts_removed == 1
    assert payloads.path_for("video.metadata", digest) is None
    with database.session() as session:
        assert session.query(Artifact).count() == 0


def test_a_recent_artifact_is_kept(tmp_path: Path, database: Database) -> None:
    clock = FakeClock()
    database, payloads, service = build(
        tmp_path, database, RetentionPolicy(maximum_age=timedelta(days=30)), clock
    )
    store(database, payloads, clock, b'{"a": 1}', "recent")

    clock.advance(timedelta(days=1))
    outcome = service.prune()

    assert outcome.artifacts_removed == 0
    with database.session() as session:
        assert session.query(Artifact).count() == 1


def test_age_is_the_only_thing_that_protects_an_artifact(
    tmp_path: Path, database: Database
) -> None:
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
        tmp_path, database, RetentionPolicy(maximum_age=timedelta(days=30)), clock
    )
    store(database, payloads, clock, b'{"v": 1}', "only-one")
    clock.advance(timedelta(days=40))

    outcome = service.prune()

    assert outcome.artifacts_removed == 1
    with database.session() as session:
        assert session.query(Artifact).count() == 0


def test_exceeding_the_size_ceiling_is_reported_rather_than_absorbed(
    tmp_path: Path,
    database: Database,
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
        database,
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


def test_staying_under_the_ceiling_is_not_reported_as_a_problem(
    tmp_path: Path, database: Database
) -> None:
    clock = FakeClock()
    database, payloads, service = build(
        tmp_path,
        database,
        RetentionPolicy(maximum_age=timedelta(days=30), maximum_bytes=10_000),
        clock,
    )
    store(database, payloads, clock, b"x" * 60, "small")

    outcome = service.prune()

    assert outcome.over_ceiling is False


def test_a_blob_with_no_artifact_row_is_swept(tmp_path: Path, database: Database) -> None:
    """Orphans are produced routinely, not exceptionally.

    `tubedepth collect` writes a payload and no row — it takes no database at
    all — so every CLI collection leaves one. Ten were sitting in the working
    store when this was written, and nothing in the system could ever have
    removed them: `prune` walks artifact rows and deletes *their* payloads, so
    a file without a row is unreachable by construction.
    """
    # Real time, not the usual fixed instant. The sweep compares `self._clock()`
    # against the file's `st_mtime`, so it is the one place in this service that
    # mixes an injectable clock with the filesystem's real one — a fake clock
    # parked in the past makes every file look like it is from the future.
    # This test used to pass by accident: START happened to sit a day behind
    # real time, and it began failing the day the calendar caught up.
    # `sweep_without_an_index` because that is literally this store: a payload
    # written by `collect`, and an index that has never held a row. The default
    # refuses it, since from inside the sweep that is indistinguishable from
    # being pointed at the wrong database.
    clock = FakeClock(datetime.now(UTC))
    database, payloads, service = build(
        tmp_path, database, RetentionPolicy(sweep_without_an_index=True), clock
    )
    stored = payloads.put("video.metadata", b'{"orphan": true}')
    # Age the file rather than the clock, for the same reason.
    _age(stored.path, timedelta(hours=2))
    assert payloads.path_for("video.metadata", stored.digest) is not None

    outcome = service.prune()

    assert payloads.path_for("video.metadata", stored.digest) is None
    assert outcome.orphans_removed == 1


def test_a_blob_written_moments_ago_is_left_alone(tmp_path: Path, database: Database) -> None:
    """The race this sweep must not lose.

    Payloads are written before their artifact row — deliberately, so a crash
    leaves an orphan rather than a row pointing at nothing. That means every
    successful collection is briefly an orphan, and a sweep with no grace
    period would delete the result of a job that is still committing.
    """
    clock = FakeClock(datetime(2026, 8, 19, tzinfo=UTC))
    database, payloads, service = build(
        tmp_path, database, RetentionPolicy(sweep_without_an_index=True), clock
    )
    stored = payloads.put("video.metadata", b'{"just": "written"}')

    outcome = service.prune()

    assert payloads.path_for("video.metadata", stored.digest) is not None
    assert outcome.orphans_removed == 0


def test_the_store_size_is_measured_on_disk_and_not_from_the_rows(
    tmp_path: Path, database: Database
) -> None:
    """The ceiling's only job is to describe the disk, so it must measure it.

    `byte_count` is the uncompressed payload size. Reporting that as the store
    size overstated the working store by five times — 4.5 MiB claimed against
    0.91 MiB of files — so a 50 GiB ceiling would have fired at roughly 10 GiB
    of actual use. Gzip is the whole reason the blob store exists; a size that
    ignores it is not a size.
    """
    clock = FakeClock(datetime(2026, 8, 19, tzinfo=UTC))
    database, payloads, service = build(tmp_path, database, RetentionPolicy(), clock)
    body = json.dumps({"text": "compressible " * 500}).encode()
    digest = store(database, payloads, clock, body, "video000001")
    stored_path = payloads.path_for("video.metadata", digest)

    outcome = service.prune()

    assert stored_path is not None
    assert outcome.total_bytes == stored_path.stat().st_size
    assert outcome.total_bytes < len(body) / 2, "gzip is why the blob store exists"


def test_an_expiring_observation_does_not_take_a_payload_a_current_one_shares(
    tmp_path: Path,
    database: Database,
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
        tmp_path, database, RetentionPolicy(maximum_age=timedelta(days=30)), clock
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


def test_a_payload_store_with_no_index_rows_is_refused_rather_than_swept(
    tmp_path: Path,
    database: Database,
) -> None:
    """An index with no rows at all cannot tell a full store from a wrong one.

    This is the shape of a database cutover half-done: the payloads are still
    on disk and the index they belong to is somewhere else. Every file is an
    orphan by the sweep's test, and the sweep is irreversible — so the one
    state where the question cannot be answered is the one state where it must
    not be guessed at.
    """
    clock = FakeClock()
    database, payloads, service = build(
        tmp_path, database, RetentionPolicy(maximum_age=timedelta(days=30)), clock
    )
    stored = payloads.put("video.metadata", b'{"orphaned": true}')
    _age(stored.path, timedelta(hours=2))

    with pytest.raises(ConfigurationError) as refusal:
        service.prune()

    assert "1" in str(refusal.value)
    assert payloads.path_for("video.metadata", stored.digest) is not None


def test_an_empty_index_and_an_empty_store_is_not_an_error(
    tmp_path: Path, database: Database
) -> None:
    """A fresh installation prunes nothing and says so, rather than refusing."""
    clock = FakeClock()
    _, _, service = build(
        tmp_path, database, RetentionPolicy(maximum_age=timedelta(days=30)), clock
    )

    outcome = service.prune()

    assert outcome.artifacts_removed == 0
    assert outcome.orphans_removed == 0


def test_the_refusal_is_overridable_for_a_store_that_is_genuinely_all_orphans(
    tmp_path: Path,
    database: Database,
) -> None:
    """`tubedepth collect` takes no database, so a collect-only host is this.

    The refusal protects a store whose index is elsewhere; it must not
    permanently strand a store that really has no index. The override is
    explicit because the operator is the only one who can tell the two apart.
    """
    # Real time, because the sweep compares the clock against the file's mtime.
    clock = FakeClock(datetime.now(UTC))
    _, payloads, service = build(
        tmp_path,
        database,
        RetentionPolicy(maximum_age=timedelta(days=30), sweep_without_an_index=True),
        clock,
    )
    stored = payloads.put("video.metadata", b'{"orphaned": true}')
    _age(stored.path, timedelta(hours=2))

    outcome = service.prune()

    assert outcome.orphans_removed == 1
    assert payloads.path_for("video.metadata", stored.digest) is None


def test_a_partially_transferred_index_refuses_the_sweep_instead_of_destroying_the_rest(
    tmp_path: Path,
    database: Database,
) -> None:
    """The other half of `_refuse_to_sweep_without_an_index`.

    A database cutover interrupted mid-`transfer` does not necessarily leave
    the target with *zero* rows — `artifacts` is the second of six tables, so
    a run that dies later leaves a handful of real rows behind. From here
    that is the same failure as the zero-row case in miniature: most of what
    is on disk has no row pointing at it, because most of the source index
    never made it across. One live row against one orphaned payload is
    already enough to trip this: `orphans >= total_rows` is parity, and a
    transfer interrupted this early reaches it by construction.
    """
    clock = FakeClock(datetime.now(UTC))
    database, payloads, service = build(
        tmp_path, database, RetentionPolicy(maximum_age=timedelta(days=30)), clock
    )
    store(database, payloads, clock, b'{"transferred": true}', "the-one-row-that-made-it")
    stranded = payloads.put("video.metadata", b'{"never_got_a_row": true}')
    _age(stranded.path, timedelta(hours=2))

    with pytest.raises(ConfigurationError) as refusal:
        service.prune()

    assert "prune" in str(refusal.value)
    assert payloads.path_for("video.metadata", stranded.digest) is not None


def test_the_disproportionate_refusal_is_overridable(
    tmp_path: Path,
    database: Database,
) -> None:
    """Same override as the zero-row case, same reason: only the operator
    can tell a corrupted pair from a store that legitimately has this shape.
    """
    clock = FakeClock(datetime.now(UTC))
    database, payloads, service = build(
        tmp_path,
        database,
        RetentionPolicy(maximum_age=timedelta(days=30), sweep_without_an_index=True),
        clock,
    )
    store(database, payloads, clock, b'{"transferred": true}', "the-one-row-that-made-it")
    stranded = payloads.put("video.metadata", b'{"never_got_a_row": true}')
    _age(stranded.path, timedelta(hours=2))

    outcome = service.prune()

    assert outcome.orphans_removed == 1
    assert payloads.path_for("video.metadata", stranded.digest) is None


def test_a_failed_commit_leaves_no_row_pointing_at_a_deleted_payload(
    tmp_path: Path,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row DELETEs must be durable before any payload file is unlinked.

    `Database.session()` commits on context exit, and a commit to a remote
    PostgreSQL can fail — a network blip, a serialization error. If the files
    were unlinked inside that still-open transaction, the rollback brings the
    expired rows back pointing at payloads that no longer exist, the exact
    state the module's write order ('a crash leaves an orphan rather than a
    row pointing at nothing') exists to forbid. Committing first means the
    same failure leaves rows *and* files untouched, and a crash after the
    commit leaves orphan files, which the next sweep collects.
    """
    clock = FakeClock()
    database, payloads, service = build(
        tmp_path, database, RetentionPolicy(maximum_age=timedelta(days=30)), clock
    )
    digest = store(database, payloads, clock, b'{"expired": true}', "old")
    clock.advance(timedelta(days=31))

    real_commit = Session.commit

    def commit_that_fails_on_row_deletion(self: Session) -> None:
        # Only the commit carrying the row DELETEs fails; the setup and
        # verification sessions above and below commit nothing but reads
        # and inserts and must keep working.
        if self.deleted:
            raise RuntimeError("simulated commit failure: the connection to PostgreSQL blipped")
        real_commit(self)

    monkeypatch.setattr(Session, "commit", commit_that_fails_on_row_deletion)

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        service.prune()

    assert payloads.path_for("video.metadata", digest) is not None, (
        "the payload was unlinked before the row DELETEs were durable — "
        "the rolled-back rows now point at nothing"
    )
    with database.session() as session:
        assert session.query(Artifact).count() == 1


def test_the_disproportion_guard_judges_the_rows_that_survive_the_age_pass(
    tmp_path: Path,
    database: Database,
) -> None:
    """The guard must see the index as the sweep will find it, not as it was.

    Issue #31's scenario: a partially interrupted transfer leaves 500 artifact
    rows — 420 of them past `maximum_age` — beside 450 stranded payload files.
    Judged against the pre-deletion count, 450 orphans against 500 rows passes
    the parity threshold; judged against the 80 rows the age pass leaves, it
    is the catastrophic shape the guard's docstring promises to refuse. The
    age deletes shrink the denominator the proportionality test depends on,
    so the decision has to be made on the post-age-pass count — and before
    anything is deleted, because in this state the 420 expired rows and their
    payloads are also data an interrupted transfer has not finished moving.
    """
    clock = FakeClock(datetime.now(UTC))
    database, payloads, service = build(
        tmp_path, database, RetentionPolicy(maximum_age=timedelta(days=30)), clock
    )
    with database.session() as session:
        for index in range(500):
            fetched = clock() - (timedelta(days=40) if index < 420 else timedelta(hours=1))
            session.add(
                Artifact(
                    kind="video.metadata",
                    target=f"video{index:06d}",
                    fingerprint=f"fp-{index:06d}",
                    digest=hashlib.sha256(f"row-{index}".encode()).hexdigest(),
                    byte_count=100,
                    fetched_at=fetched,
                    fresh_until=fetched + timedelta(hours=6),
                )
            )
    for index in range(450):
        stranded = payloads.put("video.metadata", json.dumps({"stranded": index}).encode())
        _age(stranded.path, timedelta(hours=2))

    with pytest.raises(ConfigurationError) as refusal:
        service.prune()

    assert "450" in str(refusal.value)
    assert "80" in str(refusal.value)
    assert "prune" in str(refusal.value)
    # Refused before anything irreversible: the age pass did not run either,
    # because deleting a month of rows and payloads out of a store in this
    # state is destruction of its own.
    with database.session() as session:
        assert session.query(Artifact).count() == 500
    assert sum(1 for _ in payloads.stored_files()) == 450
