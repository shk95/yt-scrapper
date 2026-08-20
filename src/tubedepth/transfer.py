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

`verify_placement()` runs on *both* ends, not only the target. The CLI
already checks the target before calling here (mirroring `_database()`), but
this function is a public entry point in its own right — every offline test
in this module calls it directly — and a hand-rolled script reaching for
`Database(url, allow_sqlite_source=True)` on both sides to move data back
after a bad cutover is exactly the moment someone is most likely to script a
direct call in a hurry, with a PostgreSQL *source* whose `search_path` nobody
checked. Putting the guard here rather than only in the CLI means every
caller gets it, not only the one that remembered to ask.

The source stays SQLite by design (Task 8): `Database` refuses any URL that
is not PostgreSQL, and `allow_sqlite_source=True` is the one deliberate
exception, reserved for a real cutover's source — the file a deployment used
to run on. `tubedepth transfer_command --to` never passes that flag, so its
target can never be SQLite; nothing here does either, since this module
constructs no `Database` of its own and only ever moves the rows the caller
already opened both ends for.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import Table, func, select
from sqlalchemy.orm import Session

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
    "moved 2,269 rows" reads the same either way. Confirmed by re-counting
    the target after every commit (see `_copy_table`), not merely echoed
    back from what was read out of the source — a count that is never
    checked on the far side is not a proof that anything arrived.
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


def _count_rows(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _refuse_a_target_that_already_holds_rows(
    target: Database, tables: list[Table], models: dict[str, type]
) -> None:
    """`artifacts` deliberately has no unique constraint on `fingerprint` —
    observations accumulate, that is the time series — so a second run would
    silently duplicate every observation and nothing would complain. Checked
    for every table before anything is written, so a table that already has
    rows stops the whole transfer rather than being caught partway through.

    This is also what makes a retry after `_copy_table` fails mid-table safe
    rather than merely detected: the failed run's committed batches are still
    sitting in the target, so the very next run this guard sees is refused
    here, before it can duplicate a single row.
    """
    with target.session(readonly=True) as session:
        for table in tables:
            model = models[table.name]
            count = _count_rows(session, model)
            if count:
                raise ConfigurationError(
                    f"target already holds {count} row(s) in {table.name!r}; "
                    "refusing to transfer into a table that is not empty"
                )


def _construct_row(model: type, row_values: dict[str, object]) -> object:
    """`model(**row_values)`, factored out as its own seam: it is the one
    step in `_copy_table` a test can fail deterministically without also
    breaking the count query `_refuse_a_target_that_already_holds_rows` and
    the post-write verifier both run against the same `model`."""
    return model(**row_values)


def _copy_table(source: Database, target: Database, table: Table, model: type) -> int:
    """Copy every row of one table, batch by batch, and confirm the target
    actually holds what was written before trusting the count.

    Two failure modes this guards against:

    A batch partway through the table can fail — a constraint the source
    never enforced, say — after earlier batches already committed. The
    exception is real and should propagate, but a bare `IntegrityError`
    tells an operator nothing about the target now holding some but not all
    of this table's rows, or that `_refuse_a_target_that_already_holds_rows`
    is what makes retrying safe rather than duplicating. Reraising with that
    said explicitly is the only difference between "read the traceback and
    guess" and "the tool told me what to do next."

    Every batch could also commit without error and still not be what
    arrived. `transfer()` is not test code and has no `expected == actual` to
    compare against; a re-count of the target against what was actually
    written is the equivalent check available at runtime, and a mismatch
    here is exactly as serious as a mismatch in the round-trip test — an
    error, not a number to print.
    """
    with source.session(readonly=True) as reader:
        source_rows = reader.scalars(select(model)).all()
        values = [
            {column.name: getattr(row, column.name) for column in table.columns}
            for row in source_rows
        ]

    written = 0
    try:
        for start in range(0, len(values), BATCH):
            batch = values[start : start + BATCH]
            with target.session() as writer:
                writer.add_all(_construct_row(model, row_values) for row_values in batch)
            written += len(batch)
    except Exception as error:
        raise ConfigurationError(
            f"transfer of {table.name!r} failed after committing {written} of "
            f"{len(values)} row(s) to the target ({error}); the target now holds "
            "partial data for this table and must be emptied before retrying"
        ) from error

    with target.session(readonly=True) as verifier:
        arrived = _count_rows(verifier, model)
    if arrived != len(values):
        raise ConfigurationError(
            f"wrote {len(values)} row(s) of {table.name!r} to the target but it now "
            f"holds {arrived}; the transfer did not verifiably arrive and the target "
            "must be emptied before retrying"
        )

    return len(values)


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
    source.verify_placement()
    target.verify_placement()

    tables = list(Base.metadata.sorted_tables)
    models = mapped_models()

    _refuse_a_target_that_already_holds_rows(target, tables, models)

    rows: dict[str, int] = {}
    for table in tables:
        model = models[table.name]
        rows[table.name] = _copy_table(source, target, table, model)

    return TransferOutcome(rows=rows)
