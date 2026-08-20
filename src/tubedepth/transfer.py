"""Carry the index from one database to another. The payloads stay put.

`TUBEDEPTH_DATA_DIR/payloads` is not in the database and does not move —
rule 7 of `docs/shared-postgres.md` is that a backup is a recovery *set*, the
index and the bytes together, and this moves one half of it. The other half
stays exactly where it is, which is why nothing here imports the payload
store: the safest way to guarantee a thing is not touched is to be unable to
reach it.

Model-driven rather than `pg_dump`-and-restore, on purpose. A dump of the
SQLite file carries its naive timestamps into `timestamptz` columns with no
conversion — `docs/status.md` §9 measured that exact shift on a downgrade,
and going through `UtcDateTime` here (Task 4) is what makes the direction
that matters, SQLite to PostgreSQL, refuse a naive value instead of silently
storing the wrong instant. Going through the ORM also means every column of
every row is compared the same way the round-trip test compares it: as the
mapped Python value, not as bytes on the wire.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import Table, func, select

from .database import Database
from .errors import ConfigurationError
from .models import Base

# The same reason `schema_versions.BATCH` is 500: a single transaction
# spanning the whole scan would hold a write lock against anything else
# running against the target for as long as the largest table takes to copy.
BATCH = 500


@dataclass(frozen=True, slots=True)
class TransferOutcome:
    """How many rows crossed, keyed by table name.

    A per-table count rather than a total: a total hides the failure that
    matters here, which is one table arriving empty while the rest move —
    "moved 2,269 rows" reads the same either way.
    """

    rows: Mapping[str, int]


def mapped_models() -> dict[str, type]:
    """Table name to the mapped class that owns it.

    `Base.metadata.sorted_tables` gives the tables in dependency order; this
    is what turns each of them back into the ORM class `transfer` constructs
    rows through. Public so `cli.transfer_command --dry-run` can count
    against the same mapping without duplicating the `local_table` narrowing.
    """
    return {
        mapper.local_table.name: mapper.class_
        for mapper in Base.registry.mappers
        if isinstance(mapper.local_table, Table)
    }


def _refuse_a_target_that_already_holds_rows(
    target: Database, tables: list[Table], models: dict[str, type]
) -> None:
    """`artifacts` deliberately has no unique constraint on `fingerprint` —
    observations accumulate, that is the time series — so a second run would
    silently duplicate every observation and nothing would complain. Checked
    for every table before anything is written, so a table that already has
    rows stops the whole transfer rather than being caught partway through.
    """
    with target.session(readonly=True) as session:
        for table in tables:
            model = models[table.name]
            count = session.scalar(select(func.count()).select_from(model))
            if count:
                raise ConfigurationError(
                    f"target already holds {count} row(s) in {table.name!r}; "
                    "refusing to transfer into a table that is not empty"
                )


def transfer(*, source: Database, target: Database) -> TransferOutcome:
    """Carry the index from `source` to `target`, table by table.

    Every row is read through the source session and reconstructed as the
    mapped class on the target session, column by column — not through the
    dialect's own wire format — which is what preserves `identifier` primary
    keys verbatim (`jobs.payload_digest` and `GET /v1/jobs/{id}/result`
    address by them) and `fetched_at` to the microsecond (the x-axis of the
    time series this milestone exists to protect) rather than letting a
    default or a dialect conversion regenerate either.
    """
    tables = list(Base.metadata.sorted_tables)
    models = mapped_models()

    _refuse_a_target_that_already_holds_rows(target, tables, models)

    rows: dict[str, int] = {}
    for table in tables:
        model = models[table.name]
        with source.session(readonly=True) as reader:
            source_rows = reader.scalars(select(model)).all()
            values = [
                {column.name: getattr(row, column.name) for column in table.columns}
                for row in source_rows
            ]

        for start in range(0, len(values), BATCH):
            batch = values[start : start + BATCH]
            with target.session() as writer:
                writer.add_all(model(**row_values) for row_values in batch)

        rows[table.name] = len(values)

    return TransferOutcome(rows=rows)
