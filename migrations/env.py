"""Alembic's entry point, wired to this project's models and settings.

The database URL is resolved through `tubedepth.settings.database_url` rather
than read from alembic.ini or kept as a second copy here — a URL in
alembic.ini is a URL in git, and two resolvers is how `tubedepth migrate` and
`tubedepth work` end up disagreeing about which database they mean without
either saying so. Batch mode, the SQLite accommodation this module used to
carry for `ALTER TABLE`, came off with the cutover (#15): PostgreSQL alters
things in place.
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import engine_from_config, pool

from tubedepth.models import Base
from tubedepth.settings import database_url

configuration = context.config
if configuration.config_file_name is not None:
    # `disable_existing_loggers` defaults to True, which switches off every
    # logger already configured. Running `tubedepth migrate` in-process would
    # otherwise silence the application's own logging for the rest of the
    # process — it silenced two unrelated tests before this argument was here.
    fileConfig(configuration.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    """Render our custom types as the plain types they are in DDL.

    `UtcDateTime` is a TypeDecorator over DateTime: it refuses naive values and
    reattaches UTC on load, which is application behaviour and not schema.
    Autogenerate would otherwise emit `tubedepth.models.UtcDateTime()` into the
    migration, which fails without an import and — worse if the import were
    added — makes every past migration depend on a class the application is
    free to rename. A migration that breaks when application code is
    refactored is one nobody can replay.

    `timezone=True` matters on its own: every `UtcDateTime` column holds an
    instant, and `docs/shared-postgres.md` rule 9 forbids `timestamp without
    time zone` — PostgreSQL's default — for that. `sa.DateTime(timezone=True)`
    renders as `timestamptz` there. Migrations only ever run against
    PostgreSQL since the cutover (#15) — `tubedepth transfer`'s SQLite source
    is never itself migrated, only read from — but the flag is harmless if
    this ever ran on SQLite regardless: SQLite has no timezone-aware storage
    either way, so the chain that dialect runs would be unaffected by it.
    """
    if type_ == "type" and type(obj).__name__ == "UtcDateTime":
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_item=render_item,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = configuration.get_section(configuration.config_ini_section) or {}
    section["sqlalchemy.url"] = database_url()
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            # Rule 1: objects a migration creates must be owned by the owner
            # role, not by whichever migrator happened to run it. Without this
            # the ownership audit finds rows and the next migrator cannot ALTER
            # what the last one created.
            connection.exec_driver_sql("SET ROLE tubedepth_owner")
            # SQLAlchemy 2.x connections auto-begin a transaction on the first
            # statement. Left open, Alembic's begin_transaction() below finds
            # one already active and nests inside it as a SAVEPOINT instead of
            # opening the real transaction it commits at the end — so the
            # whole migration would appear to succeed and then silently roll
            # back when the connection closes. Committing here (SET ROLE is a
            # session-level setting, unaffected by COMMIT) closes that
            # transaction so Alembic starts its own.
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
