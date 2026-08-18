"""Engine, connection settings, and the session context manager."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Connection, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

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

        @event.listens_for(self._engine, "begin")
        def _begin_immediate(connection: Connection) -> None:
            connection.exec_driver_sql("BEGIN IMMEDIATE")

        self._sessions = sessionmaker(bind=self._engine, expire_on_commit=False)

    def create_schema(self) -> None:
        Base.metadata.create_all(self._engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
