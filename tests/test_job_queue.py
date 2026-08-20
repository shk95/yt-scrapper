"""The durable job queue.

The queue table is also the API's read model — `GET /v1/jobs/{id}` reads the
row the worker wrote — so there is no second system that can disagree about
whether a job finished. What that buys is paid for by getting the claim exactly
right, which is what these tests are for.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import InternalError

from tubedepth.database import Database
from tubedepth.models import Job
from tubedepth.repositories import JobRepository

LEASE = timedelta(minutes=5)


def queued_database(url: str, *kinds: str) -> Database:
    """A database holding one queued job per kind, in the order given."""
    database = Database(url)
    database.create_schema()
    with database.session() as session:
        for kind in kinds:
            session.add(Job(kind=kind))
    return database


def test_claiming_an_empty_queue_returns_nothing(database: Database) -> None:
    with database.session() as session:
        assert JobRepository(session).claim(worker="worker-1", lease=LEASE) is None


def test_claiming_a_queued_job_returns_that_job(database_url_for_tests: str) -> None:
    database = queued_database(database_url_for_tests, "static.echo")

    with database.session() as session:
        claimed = JobRepository(session).claim(worker="worker-1", lease=LEASE)

    assert claimed is not None
    assert claimed.kind == "static.echo"


def test_four_workers_draining_one_queue_never_take_a_job_twice(
    database_url_for_tests: str,
) -> None:
    """Nothing is lost and nothing is doubled while draining a real queue.

    Honest about what this does not do: in-process threads contending for a
    connection pool rarely land in the actual race window on their own —
    `test_two_concurrent_claims_never_return_the_same_job` below is what pins
    the mechanism by forcing the interleaving directly. This one is a smoke
    test for the drain loop under ordinary, un-forced concurrency.
    """
    job_count = 40
    database = queued_database(
        database_url_for_tests, *(f"job.{index}" for index in range(job_count))
    )
    taken: list[str] = []
    append_lock = threading.Lock()

    def drain(worker: str) -> None:
        while True:
            with database.session() as session:
                job = JobRepository(session).claim(worker=worker, lease=LEASE)
                identifier = None if job is None else job.identifier
            if identifier is None:
                return
            with append_lock:
                taken.append(identifier)

    workers = [threading.Thread(target=drain, args=(f"worker-{n}",)) for n in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)

    assert len(taken) == len(set(taken)), "a job was claimed more than once"
    assert len(taken) == job_count, "some jobs were never claimed"


def test_only_one_of_eight_simultaneous_workers_can_claim_a_single_job(
    database_url_for_tests: str,
) -> None:
    """Eight threads released at once against a queue holding exactly one job.

    Also honest: the barrier is not enough on its own to force the race
    either — see the note on the smoke test above. Kept because it costs
    nothing and would catch a claim that returned the same row to every
    caller outright.
    """
    database = queued_database(database_url_for_tests, "static.echo")
    contenders = 8
    start = threading.Barrier(contenders)
    winners: list[str] = []
    append_lock = threading.Lock()

    def race(worker: str) -> None:
        start.wait()
        with database.session() as session:
            claimed = JobRepository(session).claim(worker=worker, lease=LEASE) is not None
        if claimed:
            with append_lock:
                winners.append(worker)

    threads = [threading.Thread(target=race, args=(f"worker-{n}",)) for n in range(contenders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(winners) == 1, f"exactly one worker may win the race, got {winners}"


def test_a_claim_works_on_a_session_that_has_already_written(
    database_url_for_tests: str,
) -> None:
    """The worker will not always claim on a virgin session.

    It records egress health, updates a heartbeat, then claims — all in one
    unit of work. This used to matter specifically because pysqlite opens an
    implicit transaction on the first write, so a claim issued afterwards
    found one already open and its own BEGIN IMMEDIATE failed — a runtime
    error inside the worker's hot loop that would not show up in any test
    that claims on a fresh session. PostgreSQL has no such failure mode (no
    per-statement BEGIN escalation to collide with), but the shape of the
    unit of work — write, then claim, same transaction — is still exactly
    what the worker does, so the test stays.
    """
    database = queued_database(database_url_for_tests, "static.echo")

    with database.session() as session:
        session.add(Job(kind="unrelated.bookkeeping"))
        session.flush()
        claimed = JobRepository(session).claim(worker="worker-1", lease=LEASE)

    assert claimed is not None


def test_two_concurrent_claims_never_return_the_same_job(database_url_for_tests: str) -> None:
    """The guarantee `Database`'s docstring argues for, forced rather than hoped for.

    `JobRepository.claim` is a SELECT for a candidate followed by a guarded
    UPDATE (`WHERE state == QUEUED`, rowcount checked). Under READ COMMITTED
    that is supposed to be enough on its own, with no BEGIN IMMEDIATE-style
    lock escalation up front: two sessions may both see the same row as
    QUEUED, but only one UPDATE can win, and the guard is what stops the
    loser from claiming a job someone else already has.

    In-process threads racing each other rarely land in that window by
    chance — session setup and the connection pool serialise them first, as
    the two tests above admit. This test does not hope for luck: a
    `before_cursor_execute` hook pauses worker A's UPDATE *after* it has
    already read the candidate as QUEUED but *before* the statement reaches
    the server, while worker B runs a complete claim — SELECT, UPDATE,
    COMMIT — to the same job. Only once B has fully committed is A released
    to send its own UPDATE. If the WHERE clause or the rowcount check were
    missing, A's UPDATE would blindly overwrite B's already-running row and
    both calls would return a job.

    Confirmed to actually discriminate: patching `claim` to skip the rowcount
    check (returning the row regardless of `taken.rowcount`) makes this test
    fail with two winners, exactly as expected.
    """
    database = queued_database(database_url_for_tests, "static.echo")

    worker_b_may_proceed = threading.Event()
    worker_a_reached_its_update = threading.Event()
    worker_a_may_send_its_update = threading.Event()

    @event.listens_for(database._engine, "before_cursor_execute")
    def _pause_worker_a_before_its_update(
        conn, cursor, statement, parameters, context, executemany
    ):  # noqa: ANN001
        if conn.info.get("role") == "A" and statement.strip().upper().startswith("UPDATE"):
            worker_a_reached_its_update.set()
            worker_a_may_send_its_update.wait(timeout=10)

    results: dict[str, Job | None] = {}

    def worker_a() -> None:
        with database.session() as session:
            session.connection().info["role"] = "A"
            results["a"] = JobRepository(session).claim(worker="A", lease=LEASE)

    def worker_b() -> None:
        assert worker_a_reached_its_update.wait(timeout=10), (
            "worker A never reached its UPDATE — the hook did not fire"
        )
        with database.session() as session:
            results["b"] = JobRepository(session).claim(worker="B", lease=LEASE)
        worker_b_may_proceed.set()

    try:
        thread_a = threading.Thread(target=worker_a)
        thread_b = threading.Thread(target=worker_b)
        thread_a.start()
        thread_b.start()
        thread_b.join(timeout=15)
        assert worker_b_may_proceed.is_set(), "worker B never completed its claim"
        worker_a_may_send_its_update.set()
        thread_a.join(timeout=15)
    finally:
        event.remove(database._engine, "before_cursor_execute", _pause_worker_a_before_its_update)

    claims = [job for job in (results.get("a"), results.get("b")) if job is not None]
    assert len(claims) == 1, f"exactly one claim may succeed for one job, got {claims}"


def test_a_readonly_session_does_not_block_a_concurrent_writer(
    database_url_for_tests: str,
) -> None:
    """The other half of the guarantee: a route that only reads must never
    queue behind a writer, or (as measured before this existed — see
    `Database`'s class docstring) `GET /healthz`'s one COUNT sits at a p99 of
    1,434 ms behind a busy worker while a route that touches no database at
    all sits at 335 ms under the same load.

    A readonly session opens its transaction and keeps it open — a real read
    in flight, not a closed one — while a concurrent, ordinary session claims
    the very job the reader just looked at. The claim must both succeed and
    return promptly; a bound of two seconds is generous for a claim that does
    one SELECT and one UPDATE against a local server, and would be blown past
    completely by even a short lock wait.

    Confirmed to actually discriminate: replacing the reader's plain SELECT
    with `LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE` inside the same
    transaction — the shape of mistake this guards against, a read path that
    somehow ends up taking a real lock — makes the claim below hang until the
    reader's transaction ends, and the two-second bound catches it.
    """
    database = queued_database(database_url_for_tests, "static.echo")

    reader_has_an_open_read = threading.Event()
    release_the_reader = threading.Event()

    def hold_a_read_open() -> None:
        with database.session(readonly=True) as session:
            session.scalars(select(Job.identifier)).all()
            reader_has_an_open_read.set()
            release_the_reader.wait(timeout=10)

    reader = threading.Thread(target=hold_a_read_open)
    reader.start()
    try:
        assert reader_has_an_open_read.wait(timeout=10), (
            "the reader never reached its open transaction"
        )

        started_at = time.monotonic()
        with database.session() as session:
            claimed = JobRepository(session).claim(worker="writer", lease=LEASE)
        elapsed = time.monotonic() - started_at
    finally:
        release_the_reader.set()
        reader.join(timeout=10)

    assert claimed is not None, "the writer could not claim while the reader's transaction was open"
    assert elapsed < 2.0, f"the writer waited {elapsed:.2f}s behind the read-only session"


def test_a_readonly_session_refuses_to_write(database: Database) -> None:
    """Otherwise `readonly` is a performance hint that silently lies: a
    session that takes no write lock but accepts writes is the one shape
    that must not exist.
    """
    with database.session() as session:
        session.add(Job(kind="static.echo"))

    with (
        pytest.raises(InternalError, match="read-only transaction"),
        database.session(readonly=True) as session,
    ):
        job = session.scalars(select(Job)).first()
        assert job is not None
        job.kind = "changed"
        session.flush()
