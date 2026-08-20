"""Engine, connection settings, and the session context manager."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


class Database:
    """A database named by a URL, and the one setting that makes it safe to claim from.

    On SQLite, every transaction is IMMEDIATE. Under SQLite's default DEFERRED
    mode a read takes only a SHARED lock and the write lock is acquired at the
    first write, so two workers can select the same job before either updates
    it — and a failed lock *upgrade* raises SQLITE_BUSY at once, ignoring
    busy_timeout entirely. IMMEDIATE takes RESERVED on the first statement
    instead. On PostgreSQL there is no equivalent hook: `JobRepository.claim`
    is a guarded UPDATE with a rowcount check, which is correct under READ
    COMMITTED without any lock escalated up front.

    Emitting this from the engine's begin event rather than inside the claim is
    what makes it a property of the database rather than something every
    repository method has to remember. It also means a claim issued after some
    earlier write in the same unit of work works: the transaction is already
    open, so nothing tries to start a second one.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._engine = create_engine(url)
        # A second engine, and it earns its keep. On SQLite the BEGIN IMMEDIATE
        # below is what makes claiming safe, but it applies to every
        # transaction the engine opens — so a route that only counts rows took
        # the write lock and queued behind the worker. WAL exists precisely so
        # readers never block writers, and one event handler was opting out of
        # it everywhere.
        #
        # Measured before this existed: 12 concurrent clients against a worker
        # running 22 transcript jobs put `GET /healthz` — one COUNT — at a p99
        # of 1,434 ms, while `GET /v1/sources`, which touches no database, sat
        # at 335 ms under the same load.
        #
        # On PostgreSQL readers never block writers in the first place — MVCC
        # means a SELECT never waits on a concurrent UPDATE's row locks — so
        # this second engine is no longer about avoiding contention. It is
        # about refusing writes and declaring intent: a route that only reads
        # cannot accidentally acquire a row lock it has no business holding,
        # and the session's own type says so rather than relying on every
        # caller remembering.
        #
        # Separate rather than a flag on the same engine because the guarantee
        # is then structural: this engine has no IMMEDIATE hook to forget, and
        # on SQLite `query_only` is set once per connection instead of per
        # transaction.
        self._read_engine = create_engine(url)
        self.dialect = self._engine.dialect.name
        self.sqlite_hooks_installed = self.dialect == "sqlite"

        if self.sqlite_hooks_installed:
            self._install_sqlite_hooks()
        else:
            self._install_read_only_hook()

        self._sessions = sessionmaker(bind=self._engine, expire_on_commit=False)
        self._read_sessions = sessionmaker(bind=self._read_engine, expire_on_commit=False)

    def _install_sqlite_hooks(self) -> None:
        """WAL, a busy timeout, foreign keys, and IMMEDIATE writes.

        SQLite-only: none of this has a PostgreSQL equivalent, and none of it
        is needed on PostgreSQL — see the class docstring and the read
        engine's comment above.
        """

        @event.listens_for(self._engine, "connect")
        @event.listens_for(self._read_engine, "connect")
        def _configure(dbapi_connection: object, record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            # WAL so the API can read job state while a worker writes, and a
            # busy timeout so a second writer waits its turn instead of raising
            # on the first collision. Both only started mattering when the
            # worker gained real concurrency.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(self._read_engine, "connect")
        def _refuse_writes(dbapi_connection: object, record: object) -> None:
            # Without this, `readonly=True` would be a performance hint that
            # silently lies: a session taking no write lock but accepting
            # writes is the one shape that must not exist, since two of them
            # can interleave exactly the way IMMEDIATE was added to prevent.
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA query_only=ON")
            cursor.close()

        @event.listens_for(self._engine, "begin")
        def _begin_immediate(connection: Connection) -> None:
            connection.exec_driver_sql("BEGIN IMMEDIATE")

    def _install_read_only_hook(self) -> None:
        """`readonly=True` refuses writes on any dialect.

        On SQLite this is `PRAGMA query_only`; here it is the transaction's own
        mode. Without it `readonly=True` would be a hint that silently lies —
        a session that declares it only reads and then writes is the one shape
        that must not exist.
        """

        @event.listens_for(self._read_engine, "begin")
        def _read_only(connection: Connection) -> None:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    def create_schema(self) -> None:
        """Create the tables the models describe, and nothing else.

        This is how tests and a fresh `--data-dir` get a database. It is not
        the deployment path — that is `tubedepth migrate`, which is the only
        thing allowed to change a schema (rule 6 of `docs/shared-postgres.md`).

        It used to also repair: add columns and indexes that appeared after a
        file was created. That closed a real gap while there was no migration
        tool, and it is now the gap — a boot that adds a column leaves
        `alembic_version` untouched, so the next upgrade tries to add a column
        that is already there.
        """
        Base.metadata.create_all(self._engine)

    def is_migrated(self) -> bool:
        """Whether a schema this expects already exists.

        `inspect` reflects the database; it issues no DDL, so calling this
        from the boot path does not reintroduce the thing #14 removed. It
        checks one table (`jobs`) rather than the whole of `Base.metadata`
        because a schema that has `jobs` but is missing something newer is
        exactly what `tubedepth migrate` exists to catch — with a real
        error naming the missing column, not a blank database pretending to
        be fine.
        """
        return inspect(self._engine).has_table("jobs")

    @contextmanager
    def session(self, *, readonly: bool = False) -> Iterator[Session]:
        """A unit of work. `readonly=True` for anything that only reads.

        The default takes the write lock on its first statement, which is what
        the claim needs and what every writer should have. A read-only session
        takes none, so it never queues behind the worker — and is refused if it
        tries to write, so the choice cannot quietly become wrong.
        """
        session = (self._read_sessions if readonly else self._sessions)()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
