"""Engine, connection settings, and the session context manager."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Column, Connection, Table, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateColumn

from .errors import ConfigurationError
from .models import Base


class Database:
    """One SQLite file, and the one setting that makes it safe to claim from.

    Every transaction is IMMEDIATE. Under SQLite's default DEFERRED mode a read
    takes only a SHARED lock and the write lock is acquired at the first write,
    so two workers can select the same job before either updates it — and a
    failed lock *upgrade* raises SQLITE_BUSY at once, ignoring busy_timeout
    entirely. IMMEDIATE takes RESERVED on the first statement instead.

    Emitting this from the engine's begin event rather than inside the claim is
    what makes it a property of the database rather than something every
    repository method has to remember. It also means a claim issued after some
    earlier write in the same unit of work works: the transaction is already
    open, so nothing tries to start a second one.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._engine = create_engine(f"sqlite+pysqlite:///{path}")
        # A second engine, and it earns its keep. The BEGIN IMMEDIATE below is
        # what makes claiming safe, but it applies to every transaction the
        # engine opens — so a route that only counts rows took the write lock
        # and queued behind the worker. WAL exists precisely so readers never
        # block writers, and one event handler was opting out of it everywhere.
        #
        # Measured before this existed: 12 concurrent clients against a worker
        # running 22 transcript jobs put `GET /healthz` — one COUNT — at a p99
        # of 1,434 ms, while `GET /v1/sources`, which touches no database, sat
        # at 335 ms under the same load.
        #
        # Separate rather than a flag on the same engine because the guarantee
        # is then structural: this engine has no IMMEDIATE hook to forget, and
        # `query_only` is set once per connection instead of per transaction.
        self._read_engine = create_engine(f"sqlite+pysqlite:///{path}")

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

        self._sessions = sessionmaker(bind=self._engine, expire_on_commit=False)
        self._read_sessions = sessionmaker(bind=self._read_engine, expire_on_commit=False)

    def create_schema(self) -> None:
        Base.metadata.create_all(self._engine)
        self._repair_existing_tables()

    def _repair_existing_tables(self) -> None:
        """Add columns that appeared after this file was first created.

        `create_all` only creates tables it does not find. A table that exists
        but has fallen behind the model is left exactly as it is, and the gap
        surfaces as `table jobs has no column named api_key_id` at the first
        INSERT — inside a worker, long after the change that caused it. There
        is no migration tool here yet, so this closes the one case that keeps
        happening: a nullable column added to a table someone already has.

        Anything else is refused by name rather than half-applied. A NOT NULL
        column with no default cannot be filled in for rows that predate it,
        and guessing a value is worse than saying which column is missing.
        """
        # One connection for the whole repair. Every transaction here is
        # IMMEDIATE, so a second connection reflecting the schema would hold
        # the write lock while this one waits for it — a self-inflicted
        # `database is locked` that only appears once the file already exists.
        with self._engine.begin() as connection:
            for table in Base.metadata.sorted_tables:
                rows = connection.exec_driver_sql(f"PRAGMA table_info({table.name})").fetchall()
                existing = {row[1] for row in rows}
                for column in table.columns:
                    if column.name in existing:
                        continue
                    self._add_column(connection, table, column)

    def _add_column(self, connection: Connection, table: Table, column: Column[object]) -> None:
        if not column.nullable and column.default is None and column.server_default is None:
            raise ConfigurationError(
                "database schema is behind the code and cannot be repaired automatically: "
                f"{table.name}.{column.name} is required and has no default"
            )
        definition = CreateColumn(column).compile(bind=self._engine)
        connection.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {definition}")

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
