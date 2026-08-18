"""Keeping the store bounded, and saying so when it is not.

Two mechanisms, deliberately different in kind.

Pruning by age is the normal path: it keeps usage proportional to what is
actually current, and it is what should hold the store far below any ceiling.

The size ceiling is a backstop. Reaching it is not an operating point — it
means the age policy is not keeping up — so it is reported rather than quietly
absorbed by evicting whatever is nearest to hand. Silent eviction turns "the
retention policy is wrong" into "some data is mysteriously missing".
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from .database import Database
from .models import Artifact, utcnow
from .payload_store import PayloadStore

logger = logging.getLogger(__name__)

DEFAULT_MAXIMUM_AGE = timedelta(days=30)
# A ceiling, not a target. Nothing here tries to fill it.
DEFAULT_MAXIMUM_BYTES = 50 * 1024**3


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    maximum_age: timedelta = DEFAULT_MAXIMUM_AGE
    maximum_bytes: int = DEFAULT_MAXIMUM_BYTES


@dataclass(frozen=True, slots=True)
class RetentionOutcome:
    artifacts_removed: int
    bytes_removed: int
    total_bytes: int
    over_ceiling: bool


class RetentionService:
    def __init__(
        self,
        *,
        database: Database,
        payloads: PayloadStore,
        policy: RetentionPolicy | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._database = database
        self._payloads = payloads
        self._policy = policy or RetentionPolicy()
        self._clock = clock

    def prune(self) -> RetentionOutcome:
        cutoff = self._clock() - self._policy.maximum_age
        removed = 0
        freed = 0

        with self._database.session() as session:
            artifacts = session.scalars(select(Artifact)).all()
            # Age alone, with nothing protected. An earlier version kept the
            # newest observation of each question regardless of age, on the
            # theory that a stale answer beats none — but a stale artifact is
            # never served: `fresh()` filters on fresh_until, and the longest
            # freshness here is thirty days for captions. Protecting it bought
            # no cache hits and cost unbounded growth, because the store would
            # then grow with the number of distinct things ever collected.
            #
            # What maximum_age therefore buys is a bounded window of history:
            # how a video's counts moved over the last month is free, and older
            # than that is not kept.
            total = 0
            for artifact in artifacts:
                if artifact.fetched_at >= cutoff:
                    total += artifact.byte_count
                    continue
                self._payloads.delete(artifact.kind, artifact.digest)
                session.delete(artifact)
                removed += 1
                freed += artifact.byte_count

        over = total > self._policy.maximum_bytes
        if over:
            logger.warning(
                "artifact store is %.1f GiB, over the %.1f GiB ceiling — "
                "the age policy is not keeping up",
                total / 1024**3,
                self._policy.maximum_bytes / 1024**3,
            )
        if removed:
            logger.info("pruned %s artifact(s), freeing %.1f MiB", removed, freed / 1024**2)

        return RetentionOutcome(
            artifacts_removed=removed,
            bytes_removed=freed,
            total_bytes=total,
            over_ceiling=over,
        )
