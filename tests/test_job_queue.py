"""The durable job queue.

The queue table is also the API's read model — `GET /v1/jobs/{id}` reads the
row the worker wrote — so there is no second system that can disagree about
whether a job finished. What that buys is paid for by getting the claim exactly
right, which is what these tests are for.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from tubedepth.database import Database
from tubedepth.models import Job
from tubedepth.repositories import JobRepository

LEASE = timedelta(minutes=5)


def queued_database(path: Path, *kinds: str) -> Database:
    """A database holding one queued job per kind, in the order given.

    Takes a directory rather than the `database_url_for_tests` fixture: this
    module also opens the same file with raw `sqlite3` (the write-lock tests
    below), so the path has to be known, not just the URL.
    """
    database = Database(f"sqlite+pysqlite:///{path / 'tubedepth.db'}")
    database.create_schema()
    with database.session() as session:
        for kind in kinds:
            session.add(Job(kind=kind))
    return database


def test_claiming_an_empty_queue_returns_nothing(database: Database) -> None:
    with database.session() as session:
        assert JobRepository(session).claim(worker="worker-1", lease=LEASE) is None


def test_claiming_a_queued_job_returns_that_job(tmp_path: Path) -> None:
    database = queued_database(tmp_path, "static.echo")

    with database.session() as session:
        claimed = JobRepository(session).claim(worker="worker-1", lease=LEASE)

    assert claimed is not None
    assert claimed.kind == "static.echo"


def test_four_workers_draining_one_queue_never_take_a_job_twice(tmp_path: Path) -> None:
    """Nothing is lost and nothing is doubled while draining a real queue.

    Honest about what this does not do: it was measured against an
    implementation with the write lock removed and with the compare-and-swap
    guard removed, and passed ten times out of ten in both cases. Session setup
    costs far more than the race window, so the threads never actually
    interleave. What pins the mechanism is
    test_a_transaction_holds_the_write_lock_from_its_very_first_statement;
    this one is a smoke test for the drain loop.
    """
    job_count = 40
    database = queued_database(tmp_path, *(f"job.{index}" for index in range(job_count)))
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


def test_only_one_of_eight_simultaneous_workers_can_claim_a_single_job(tmp_path: Path) -> None:
    """Eight threads released at once against a queue holding exactly one job.

    Also honest: the barrier was not enough either. This too passes with the
    write lock removed. In-process contention on this machine cannot force two
    SELECTs into the same window — the GIL and the connection pool serialise
    the threads first. Kept because it costs nothing and would catch a claim
    that returned the same row to every caller outright.
    """
    database = queued_database(tmp_path, "static.echo")
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


def test_a_claim_works_on_a_session_that_has_already_written(tmp_path: Path) -> None:
    """The worker will not always claim on a virgin session.

    It records egress health, updates a heartbeat, then claims — all in one
    unit of work. pysqlite opens an implicit transaction on the first write, so
    a claim issued afterwards finds one already open and its own BEGIN
    IMMEDIATE fails. That is a runtime error inside the worker's hot loop, and
    it will not show up in any test that claims on a fresh session.
    """
    database = queued_database(tmp_path, "static.echo")

    with database.session() as session:
        session.add(Job(kind="unrelated.bookkeeping"))
        session.flush()
        claimed = JobRepository(session).claim(worker="worker-1", lease=LEASE)

    assert claimed is not None


def test_a_transaction_holds_the_write_lock_from_its_very_first_statement(tmp_path: Path) -> None:
    """The guarantee, asserted directly rather than inferred from silence.

    Under SQLite's default DEFERRED transaction a read takes only a SHARED
    lock, so a second connection may still write — which is precisely the
    window in which two workers select the same job before either updates it.
    IMMEDIATE takes RESERVED up front, and an outside writer is refused.

    Without this test, an engine that quietly lost its IMMEDIATE setting still
    passes every other test in this file: nothing errors, the claim still
    works, and the race window is simply open again.
    """
    database = queued_database(tmp_path, "static.echo")

    with database.session() as session:
        session.scalars(select(Job.identifier)).all()

        outsider = sqlite3.connect(tmp_path / "tubedepth.db", timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                outsider.execute("UPDATE jobs SET kind = 'stolen'")
        finally:
            outsider.close()


def test_a_read_only_session_does_not_take_the_write_lock(tmp_path: Path) -> None:
    """The other half of the guarantee above, and it was missing.

    The engine emits BEGIN IMMEDIATE for *every* transaction, which is what
    makes the claim safe. It also made every API read a writer: a route that
    only counts queued jobs took RESERVED and queued behind the worker.

    Measured before this existed — 12 concurrent clients against a worker
    running 22 transcript jobs — `GET /healthz`, which runs one COUNT, had a
    p99 of 1,434 ms while `GET /v1/sources`, which touches no database at all,
    had 335 ms at the same concurrency. WAL exists so readers never block; we
    were opting out of it on every route.
    """
    database = queued_database(tmp_path, "static.echo")

    with database.session(readonly=True) as session:
        session.scalars(select(Job.identifier)).all()

        outsider = sqlite3.connect(tmp_path / "tubedepth.db", timeout=0)
        try:
            outsider.execute("UPDATE jobs SET kind = 'written-while-read'")
            outsider.commit()
        finally:
            outsider.close()


def test_a_read_only_session_refuses_to_write(tmp_path: Path) -> None:
    """Otherwise `readonly` is a performance hint that silently lies.

    A session that takes no write lock but accepts writes is the one shape
    that must not exist: it would let two of them interleave exactly the way
    IMMEDIATE was added to prevent.
    """
    database = queued_database(tmp_path, "static.echo")

    # OperationalError specifically, and the SQLite message: a plain `Exception`
    # match passed against the TypeError from the missing keyword while this was
    # being written, which is a green test proving nothing.
    with (
        pytest.raises(OperationalError, match="readonly database"),
        database.session(readonly=True) as session,
    ):
        job = session.scalars(select(Job)).first()
        assert job is not None
        job.kind = "changed"
        session.flush()
