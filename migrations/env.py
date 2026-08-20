"""Alembic's entry point, wired to this project's models and settings.

Two things here are not boilerplate. The database URL is resolved rather than
read from alembic.ini, so a committed file cannot point a migration at the
wrong database. And batch mode is on, because SQLite cannot ALTER most things
in place — without it a column rename or a constraint change fails at the
moment it is needed rather than at the moment it is written.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import engine_from_config, pool

from tubedepth.models import Base

configuration = context.config
if configuration.config_file_name is not None:
    # `disable_existing_loggers` defaults to True, which switches off every
    # logger already configured. Running `tubedepth migrate` in-process would
    # otherwise silence the application's own logging for the rest of the
    # process — it silenced two unrelated tests before this argument was here.
    fileConfig(configuration.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

DEFAULT_DATA_DIRECTORY = Path(os.environ.get("TUBEDEPTH_DATA_DIR", "var"))


def database_url() -> str:
    """Where to migrate, in the same order the application decides it.

    A URL in alembic.ini is a URL in git, and the first time someone runs a
    migration from a checkout it points at whichever database that file
    happened to name.
    """
    explicit = os.environ.get("TUBEDEPTH_DATABASE_URL")
    if explicit:
        return explicit
    return f"sqlite+pysqlite:///{DEFAULT_DATA_DIRECTORY / 'tubedepth.db'}"


def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    """Render our custom types as the plain types they are in DDL.

    `UtcDateTime` is a TypeDecorator over DateTime: it refuses naive values and
    reattaches UTC on load, which is application behaviour and not schema.
    Autogenerate would otherwise emit `tubedepth.models.UtcDateTime()` into the
    migration, which fails without an import and — worse if the import were
    added — makes every past migration depend on a class the application is
    free to rename. A migration that breaks when application code is
    refactored is one nobody can replay.
    """
    if type_ == "type" and type(obj).__name__ == "UtcDateTime":
        return "sa.DateTime()"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
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
            # SQLite cannot ALTER most things in place; batch mode copies the
            # table through a new one. Without it the first migration that
            # renames a column or changes a constraint fails when it is run
            # rather than when it is written.
            render_as_batch=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
