"""Which normalizer wrote a stored payload, for rows written before we recorded it.

`Artifact.schema_version` is written from now on. Rows that predate the column
hold only a fingerprint, which is a SHA-256 over kind, target, version and
parameters together — not reversible. So attribution works forwards: recompute
the hash for each version the kind could have been at and see which one agrees.

That makes a match a proof rather than a guess. Two candidates cannot both
match, and a wrong candidate cannot match by accident. The only reachable error
is no candidate matching, and such a row is left alone and counted. Stamping it
with the source's current version would be worse than leaving it blank — it
would tell `channel.about`'s v1 rows, whose contents are known to be wrong, that
they came from the version that fixed them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select

from .database import Database
from .errors import TubedepthError
from .fingerprints import fingerprint
from .models import Artifact
from .sources import SourceRegistry, default_registry

logger = logging.getLogger(__name__)

# Every schema_version a kind has been collected under *before* its current one.
# A source knows only what it is now, and a fingerprint gives nothing back, so
# the history has to be written down. Once a version leaves this mapping the
# payloads written under it can no longer be attributed by any means — which is
# why it is maintained at the moment of the bump rather than archaeologically
# afterwards. See docs/releasing.md, where the bump instruction lives.
PREVIOUS_VERSIONS: Mapping[str, tuple[str, ...]] = {
    "channel.about": ("1",),
}

# How many rows to update per write transaction. Every transaction here is
# IMMEDIATE, so one session spanning the whole scan would hold the write lock
# against the worker for its entire duration.
BATCH = 500


@dataclass(frozen=True, slots=True)
class BackfillOutcome:
    attributed: int
    # By kind, because "eleven rows could not be attributed" is a shrug and
    # "eleven channel.about rows could not be attributed" is a version missing
    # from PREVIOUS_VERSIONS.
    unattributed: Mapping[str, int]
    scanned: int


class SchemaVersionBackfill:
    def __init__(self, *, database: Database, registry: SourceRegistry | None = None) -> None:
        self._database = database
        self._registry = registry or default_registry()

    def _candidates(self, kind: str) -> tuple[str, ...]:
        try:
            source = self._registry.get(kind)
        except TubedepthError:
            # A kind this build no longer has. Its rows are exactly the history
            # this is meant to preserve, so they are reported, not raised on.
            return ()
        return (*PREVIOUS_VERSIONS.get(kind, ()), source.schema_version)

    def _attribute(self, artifact: Artifact) -> str | None:
        """The version whose fingerprint agrees with the one on the row.

        Recomputed with an **empty** parameter mapping, never with what the
        source declares today. Every row missing a version predates the column,
        and every row that predates the column was keyed before parameters
        entered the key at all — so using the current declaration matches
        nothing for exactly the kinds whose fingerprints have since moved, and
        silently leaves them blank while attributing everything else.
        """
        for version in self._candidates(artifact.kind):
            if artifact.fingerprint == fingerprint(
                kind=artifact.kind, target=artifact.target, schema_version=version
            ):
                return version
        return None

    def run(self, *, dry_run: bool = False) -> BackfillOutcome:
        """Attribute every row that does not name its version yet.

        Selecting on NULL is what makes this safe to run at any time, including
        beside a busy worker: a row the worker writes carries its version by
        construction, so it is never a candidate and never contended for.
        """
        with self._database.session(readonly=True) as session:
            pending = [
                (artifact.identifier, artifact.kind, artifact.target, artifact.fingerprint)
                for artifact in session.scalars(
                    select(Artifact).where(Artifact.schema_version.is_(None))
                ).all()
            ]

        resolved: dict[str, str] = {}
        unattributed: dict[str, int] = {}
        for identifier, kind, target, question in pending:
            version = self._attribute(
                Artifact(kind=kind, target=target, fingerprint=question, digest="", byte_count=0)
            )
            if version is None:
                unattributed[kind] = unattributed.get(kind, 0) + 1
            else:
                resolved[identifier] = version

        if not dry_run:
            identifiers = list(resolved)
            for start in range(0, len(identifiers), BATCH):
                with self._database.session() as session:
                    for identifier in identifiers[start : start + BATCH]:
                        artifact = session.get(Artifact, identifier)
                        if artifact is not None:
                            artifact.schema_version = resolved[identifier]

        if unattributed:
            logger.warning(
                "could not attribute %s artifact(s) to a schema version: %s",
                sum(unattributed.values()),
                unattributed,
            )
        return BackfillOutcome(
            attributed=len(resolved), unattributed=unattributed, scanned=len(pending)
        )
