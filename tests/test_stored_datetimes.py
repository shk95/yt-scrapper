"""Datetimes must come back the way they went in.

SQLite has no datetime type and no timezone, so a value written as aware UTC
reads back naive unless something puts the offset on again. Everything in this
project treats stored instants as aware — the contract says so — and a naive
one does not raise, it silently compares wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import StatementError

from tubedepth.database import Database
from tubedepth.models import Artifact


def test_a_stored_instant_reads_back_offset_aware(database: Database) -> None:
    written = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

    with database.session() as session:
        session.add(
            Artifact(
                kind="video.metadata",
                target="dQw4w9WgXcQ",
                fingerprint="fp",
                digest="d",
                byte_count=1,
                fetched_at=written,
                fresh_until=written + timedelta(hours=6),
            )
        )

    with database.session() as session:
        artifact = session.query(Artifact).one()
        assert artifact.fetched_at.tzinfo is not None
        assert artifact.fetched_at == written
        # And it is usable in the comparison that matters, without raising.
        assert artifact.fresh_until > datetime.now(UTC) - timedelta(days=365 * 100)


def test_a_naive_instant_is_refused(database: Database) -> None:
    """The other half of this class's contract. `UtcDateTime` refusing a
    naive value in Python is what makes the application correct while
    Task 4 found the column type itself was not (`docs/status.md` §9) — and
    that guarantee had no test until now.
    """
    naive = datetime(2026, 8, 18, 12, 0)  # no tzinfo

    # `process_bind_param`'s ValueError fires when SQLAlchemy actually binds
    # the parameter, which wraps it in StatementError rather than letting it
    # propagate bare.
    with pytest.raises(StatementError, match="naive"), database.session() as session:
        session.add(
            Artifact(
                kind="video.metadata",
                target="dQw4w9WgXcQ",
                fingerprint="fp",
                digest="d",
                byte_count=1,
                fetched_at=naive,
                fresh_until=naive + timedelta(hours=6),
            )
        )
        session.flush()
