"""Engine, connection settings, and the session context manager."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, create_engine, event, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from .errors import ConfigurationError
from .models import Base

# docs/shared-postgres.md rule 4: the connection budget is a number written
# down, not an assertion, and it is cluster-wide (roles and max_connections
# are cluster-global even though each service now has its own database) —
# `deploy/service-manifest.yaml` declares 32 as the ceiling this service was
# granted by the fleet on request (#26) (also `CONNECTION LIMIT 32` on
# `tubedepth_runtime` in deploy/postgres-bootstrap.sql, so a miscount here is
# caught by the database itself rather than trusted). Sizing a pool bigger
# than what fits inside that 32 is not this module's call to make — see
# `deploy/service-manifest.yaml` for why `TUBEDEPTH_CONCURRENCY`'s deployed
# default is 6, the AIMD controller's measured useful ceiling, not raised to
# whatever the pool math would otherwise support.
#
# The API process holds two engines at this default ceiling — a writer and a
# reader (see the class docstring for why they are separate):
#
#   ceiling per engine = pool_size + max_overflow = 2 + 2 = 4
#   API total          = 2 engines x 4            = 8
#
# The worker process is not symmetric with the API any more. Its write engine
# is what `deploy/tubedepth-worker.service`'s `TUBEDEPTH_CONCURRENCY` actually
# sizes — see `_write_pool_kwargs` — because `Worker.drain` runs one claim
# thread and one lease-renewal thread per unit of concurrency
# (`worker.py`'s `pump` and `_holding_lease`), and both take a session on the
# *write* engine. Measured directly at concurrency 8 (`docs/status.md`): up to
# 16 simultaneous demands on a pool sized for 4 serialize into batches that
# each wait roughly one session's hold time behind the one before it — real
# under load, not merely a thread-count guess. That is why the pool now scales
# with concurrency at all; it is not a reason to deploy above the AIMD
# controller's measured useful ceiling of 6 (see `deploy/service-manifest.yaml`
# and `docs/status.md`) — past that the bottleneck is YouTube's side, not this
# pool's. The worker's read engine is not part of
# that burst (`Worker` takes at most one readonly session at a time, in
# `reap()`) and stays at the default ceiling regardless of concurrency.
#
# `deploy/service-manifest.yaml` carries the full worked arithmetic and the
# migration-connection and rolling-deploy-overlap terms alongside it.
_POOL_SIZE = 2
_MAX_OVERFLOW = 2


def _write_pool_kwargs(dialect: str, *, pool_size: int, max_overflow: int) -> dict[str, int]:
    return {"pool_size": pool_size, "max_overflow": max_overflow} if dialect == "postgresql" else {}


class Database:
    """A database named by a URL, and the one setting that makes it safe to claim from.

    PostgreSQL only, since the cutover (#15) — the constructor refuses any
    other dialect, with one deliberate exception. `tubedepth transfer` reads
    the index out of a SQLite file as the one-time act of carrying it onto
    PostgreSQL, so its *source* endpoint passes `allow_sqlite_source=True`;
    every other caller, including the application itself, gets the refusal.
    See `transfer.py` and `docs/status.md` for why the source stays SQLite
    while nothing else does.

    `JobRepository.claim` is a guarded UPDATE (`state == QUEUED` in the WHERE
    clause) with a rowcount check, which is correct under READ COMMITTED with
    no lock escalated up front: two workers can both SELECT the same
    candidate, but only one UPDATE matches a still-QUEUED row — the other
    affects zero rows and returns None. That is the entire safety mechanism;
    nothing here escalates a lock ahead of it the way SQLite's BEGIN IMMEDIATE
    used to.
    """

    SCHEMA = "tubedepth"

    def __init__(
        self,
        url: str,
        *,
        allow_sqlite_source: bool = False,
        pool_size: int = _POOL_SIZE,
        max_overflow: int = _MAX_OVERFLOW,
    ) -> None:
        """`pool_size`/`max_overflow` size the *write* engine only.

        Every caller but one wants the default (`_POOL_SIZE`/`_MAX_OVERFLOW`,
        what the budget arithmetic above is spent against). `tubedepth work`
        is the exception: it passes a ceiling derived from `--concurrency`,
        because that process — not the API — is the one whose thread count
        actually determines how many sessions it can want at once. See
        `deploy/service-manifest.yaml` for the accounting this feeds.
        """
        self._url = url
        dialect = make_url(url).get_backend_name()
        if dialect != "postgresql" and not allow_sqlite_source:
            raise ConfigurationError(
                f"tubedepth runs on PostgreSQL only since the cutover (#15); got a "
                f"{dialect!r} URL. `tubedepth transfer --from` is the one place a "
                "SQLite source is still accepted, for carrying an old index across."
            )
        # Explicit pool ceilings only on PostgreSQL. A SQLite source opened
        # through `allow_sqlite_source` is a one-shot local file read by
        # `tubedepth transfer`, not a budget any other service on a shared
        # server draws from, so it gets no ceiling.
        pool_kwargs = _write_pool_kwargs(dialect, pool_size=pool_size, max_overflow=max_overflow)
        self._engine = create_engine(url, **pool_kwargs)
        # A second engine, and it earns its keep: a route that only reads
        # cannot accidentally acquire a row lock it has no business holding,
        # and the session's own type says so rather than relying on every
        # caller remembering. On PostgreSQL readers never block writers in the
        # first place — MVCC means a SELECT never waits on a concurrent
        # UPDATE's row locks — so this is about refusing writes and declaring
        # intent, not about avoiding contention.
        #
        # Measured before the read/write split existed: 12 concurrent clients
        # against a worker running 22 transcript jobs put `GET /healthz` — one
        # COUNT — at a p99 of 1,434 ms, while `GET /v1/sources`, which touches
        # no database, sat at 335 ms under the same load. That measurement was
        # taken on SQLite, where every transaction was IMMEDIATE and a reader
        # took the write lock; the split stayed because the guarantee it buys
        # — a session that claims `readonly=True` and then writes is refused,
        # not merely unlikely — does not depend on which dialect is running.
        # The read engine keeps the default ceiling regardless of what the
        # write engine was given: nothing takes more than one readonly
        # session at a time (`Worker.reap`), so it is not where concurrency
        # burst pressure lands.
        read_pool_kwargs = _write_pool_kwargs(
            dialect, pool_size=_POOL_SIZE, max_overflow=_MAX_OVERFLOW
        )
        self._read_engine = create_engine(url, **read_pool_kwargs)
        self.dialect = dialect

        if self.dialect == "postgresql":
            self._install_read_only_hook()

        self._sessions = sessionmaker(bind=self._engine, expire_on_commit=False)
        self._read_sessions = sessionmaker(bind=self._read_engine, expire_on_commit=False)

    def _install_read_only_hook(self) -> None:
        """`readonly=True` refuses writes, on PostgreSQL.

        A no-op on a SQLite source opened through `allow_sqlite_source`:
        `tubedepth transfer` is the only caller that ever sees one, it is a
        one-shot local read, and `SET TRANSACTION READ ONLY` is PostgreSQL
        syntax with no SQLite equivalent. Without this hook, `readonly=True`
        would otherwise be a hint that silently lies on the dialect that
        matters — a session that declares it only reads and then writes is
        the one shape that must not exist.
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

    def verify_placement(self) -> None:
        """Refuse to run against a connection whose search_path is not ours.

        Unqualified names resolve through `search_path`, so this one setting is
        what puts this service's tables — and `alembic_version` — in its own
        schema rather than in the `public` one three other services share. It
        is set by `ALTER ROLE` in `deploy/postgres-bootstrap.sql`, and skipping
        that line fails nothing: the tables are simply created somewhere else,
        and the damage is only visible when another service's migration meets
        them.

        Checked at startup rather than per session: one query, at the moment it
        can still be fixed, before any table exists — so this runs first in
        `cli._database()`, ahead of `is_migrated()`. A wrong `search_path` and
        a missing migration are different diagnoses: with the wrong
        `search_path`, `is_migrated()` would also report False (`jobs` really
        is invisible from here), and telling the operator to run
        `tubedepth migrate` would be wrong — the schema may already exist and
        be fully migrated, just unreachable from this connection.

        A no-op on SQLite, which has no `search_path` and no server-side role
        to misconfigure. The leading entry may be quoted (`SHOW search_path`
        quotes identifiers that need it) and the bootstrap's value carries a
        second entry (`tubedepth, pg_catalog`) after the schema name, both of
        which must be accepted rather than refused.
        """
        if self.dialect != "postgresql":
            return
        with self._read_engine.connect() as connection:
            path = connection.exec_driver_sql("SHOW search_path").scalar_one()
        leading = path.split(",")[0].strip().strip('"')
        if leading != self.SCHEMA:
            raise ConfigurationError(
                f"this connection's search_path leads with {leading!r}, not {self.SCHEMA!r}: "
                f"unqualified tables would be created in the shared schema. Run "
                f"ALTER ROLE <role> IN DATABASE <db> SET search_path = {self.SCHEMA}, pg_catalog "
                f"(deploy/postgres-bootstrap.sql does this)"
            )

    def is_migrated(self) -> bool:
        """Whether a schema this expects already exists.

        `inspect` reflects the database; it issues no DDL, so calling this
        from the boot path does not reintroduce the thing #14 removed. It
        checks one table (`jobs`) rather than the whole of `Base.metadata`
        because a schema that has `jobs` but is missing something newer is
        exactly what `tubedepth migrate` exists to catch — with a real
        error naming the missing column, not a blank database pretending to
        be fine.

        On PostgreSQL the schema is passed explicitly rather than left to
        `search_path` resolution. `tubedepth_migrator` is `NOINHERIT` (rule 1)
        and holds no direct `USAGE` on `tubedepth` — only `tubedepth_owner`
        does, and the migrator only acts as owner through the explicit
        `SET ROLE` `migrations/env.py` performs for DDL. A plain connection as
        the migrator therefore resolves `current_schema()` to `pg_catalog`,
        since PostgreSQL silently drops a `search_path` entry the role has no
        `USAGE` on when picking the implicit schema for an unqualified name —
        so an unqualified `has_table` read `False` on a fully migrated
        database, and `tubedepth migrate`, run against the very credential
        `env.py` uses `SET ROLE` for, printed a `✓` from the upgrade and a
        `✗ no schema at …` from the post-migrate check in the same run. Naming
        the schema sidesteps that: PostgreSQL's catalog rows are visible
        without `USAGE` on the containing schema — only *reading data* out of
        it needs the grant — so `has_table("jobs", schema=self.SCHEMA)` is
        correct without a `SET ROLE`, for every caller of `_database()`, not
        only the one `migrate` happens to run right after an upgrade.
        """
        if self.dialect == "postgresql":
            return inspect(self._engine).has_table("jobs", schema=self.SCHEMA)
        return inspect(self._engine).has_table("jobs")

    @contextmanager
    def session(self, *, readonly: bool = False) -> Iterator[Session]:
        """A unit of work. `readonly=True` for anything that only reads.

        The default opens through the write engine, which is what a claim's
        row lock needs and what every writer should have. A read-only session
        goes through the separate read engine instead, on PostgreSQL refused
        outright if it tries to write (`_install_read_only_hook`), so the
        choice cannot quietly become wrong.
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
