"""Attributing a stored payload to the normalizer that wrote it.

The column is written from now on. What it cannot do is answer for rows that
predate it, and those are recovered the only way a SHA-256 allows: recompute
the fingerprint for each version the kind could have been at and see which one
matches. A match is a proof, not a guess — the hash covers kind, target,
version and parameters together, so two candidates cannot both match and a
wrong candidate cannot match by accident.

The failure that is possible is no candidate matching, and the whole discipline
of this file is that such a row is left alone and counted rather than stamped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tubedepth.database import Database
from tubedepth.fingerprints import fingerprint
from tubedepth.models import Artifact
from tubedepth.schema_versions import SchemaVersionBackfill

WHEN = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def store(
    database: Database, *, kind: str, target: str, question: str, version: str | None
) -> None:
    with database.session() as session:
        session.add(
            Artifact(
                kind=kind,
                target=target,
                fingerprint=question,
                schema_version=version,
                digest="d" * 64,
                byte_count=1,
                fetched_at=WHEN,
                fresh_until=WHEN + timedelta(hours=6),
            )
        )


def prepared(tmp_path: Path) -> Database:
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    return database


def versions(database: Database) -> list[str | None]:
    with database.session() as session:
        return [artifact.schema_version for artifact in session.query(Artifact).all()]


def test_a_row_written_before_the_column_existed_is_attributed(tmp_path: Path) -> None:
    database = prepared(tmp_path)
    store(
        database,
        kind="video.metadata",
        target="dQw4w9WgXcQ",
        question=fingerprint(kind="video.metadata", target="dQw4w9WgXcQ", schema_version="1"),
        version=None,
    )

    outcome = SchemaVersionBackfill(database=database).run()

    assert outcome.attributed == 1
    assert versions(database) == ["1"]


def test_a_listing_row_from_before_parameters_entered_the_key_is_still_attributed(
    tmp_path: Path,
) -> None:
    """The test that catches the tempting wrong implementation.

    `channel.videos` now declares `{"limit": 100}`, so the obvious move is to
    pass the source's current parameters — they are right there. Every row that
    predates the column was keyed with an empty mapping, whatever its kind, so
    that matches nothing for exactly the six kinds whose fingerprints just
    moved, and leaves them unattributed while confidently attributing the rest.
    """
    database = prepared(tmp_path)
    store(
        database,
        kind="channel.videos",
        target="@someone",
        question=fingerprint(kind="channel.videos", target="@someone", schema_version="1"),
        version=None,
    )

    outcome = SchemaVersionBackfill(database=database).run()

    assert outcome.attributed == 1, f"unattributed by kind: {dict(outcome.unattributed)}"
    assert versions(database) == ["1"]


def test_a_row_from_a_superseded_version_is_not_stamped_with_the_current_one(
    tmp_path: Path,
) -> None:
    """`channel.about` is at "2", and its v1 rows hold a video's description
    where the channel's belongs. Stamping those "2" would erase the one signal
    that says the contents are wrong."""
    database = prepared(tmp_path)
    store(
        database,
        kind="channel.about",
        target="@someone",
        question=fingerprint(kind="channel.about", target="@someone", schema_version="1"),
        version=None,
    )

    SchemaVersionBackfill(database=database).run()

    assert versions(database) == ["1"]


def test_a_row_matching_no_known_version_is_left_alone_and_named(tmp_path: Path) -> None:
    """Never guess. Unattributed and honest beats attributed and wrong."""
    database = prepared(tmp_path)
    store(
        database,
        kind="video.metadata",
        target="dQw4w9WgXcQ",
        question="not a fingerprint this project ever produced",
        version=None,
    )

    outcome = SchemaVersionBackfill(database=database).run()

    assert outcome.attributed == 0
    assert dict(outcome.unattributed) == {"video.metadata": 1}
    assert versions(database) == [None]


def test_a_row_that_already_names_its_version_is_left_alone(tmp_path: Path) -> None:
    """Idempotent, and the reason a worker writing concurrently is not a race:
    a row it writes is non-NULL by construction and invisible to this."""
    database = prepared(tmp_path)
    store(
        database,
        kind="video.metadata",
        target="dQw4w9WgXcQ",
        question=fingerprint(kind="video.metadata", target="dQw4w9WgXcQ", schema_version="1"),
        version="9",
    )

    outcome = SchemaVersionBackfill(database=database).run()

    assert outcome.attributed == 0
    assert versions(database) == ["9"]


def test_a_dry_run_reports_what_it_would_do_and_writes_nothing(tmp_path: Path) -> None:
    database = prepared(tmp_path)
    store(
        database,
        kind="video.metadata",
        target="dQw4w9WgXcQ",
        question=fingerprint(kind="video.metadata", target="dQw4w9WgXcQ", schema_version="1"),
        version=None,
    )

    outcome = SchemaVersionBackfill(database=database).run(dry_run=True)

    assert outcome.attributed == 1
    assert versions(database) == [None]


def test_a_kind_no_source_is_registered_for_is_reported_rather_than_raising(
    tmp_path: Path,
) -> None:
    """A retired kind's rows are exactly the history this is meant to preserve."""
    database = prepared(tmp_path)
    store(
        database,
        kind="video.dislikes",
        target="dQw4w9WgXcQ",
        question="whatever it was",
        version=None,
    )

    outcome = SchemaVersionBackfill(database=database).run()

    assert dict(outcome.unattributed) == {"video.dislikes": 1}
