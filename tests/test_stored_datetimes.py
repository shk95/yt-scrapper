"""Datetimes must come back the way they went in.

SQLite has no datetime type and no timezone, so a value written as aware UTC
reads back naive unless something puts the offset on again. Everything in this
project treats stored instants as aware — the contract says so — and a naive
one does not raise, it silently compares wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tubedepth.database import Database
from tubedepth.models import Artifact


def test_a_stored_instant_reads_back_offset_aware(tmp_path: Path) -> None:
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
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
