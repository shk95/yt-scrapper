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
from datetime import UTC, datetime, timedelta

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
    # How long a payload with no artifact row is left alone. Long enough that a
    # collection committing its row is never mistaken for rubbish; short enough
    # that a crashed worker's leftovers do not accumulate for a day.
    orphan_grace: timedelta = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class RetentionOutcome:
    artifacts_removed: int
    bytes_removed: int
    # On disk, gzipped, as the filesystem sees it — not the sum of byte_count,
    # which is the uncompressed size and overstated the working store fivefold.
    total_bytes: int
    over_ceiling: bool
    orphans_removed: int = 0


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

    def _sweep_orphans(self, live: set[str]) -> tuple[int, int]:
        """Delete payloads no artifact row points at, and total what remains.

        These are produced routinely rather than exceptionally: `tubedepth
        collect` takes no database, so every CLI collection leaves one. Ten
        were sitting in the working store when this was written and nothing in
        the system could ever have removed them — `prune` walks rows and
        deletes *their* payloads, so a file without a row is unreachable.

        The grace period is the part that matters. Payloads are written before
        their row, deliberately, so that a crash leaves an orphan rather than a
        row pointing at nothing. Every successful collection is therefore
        briefly an orphan, and a sweep without a grace period would delete the
        result of a job that is still committing.

        The total returned is what the filesystem holds after the sweep, which
        is what the ceiling is about. Summing `byte_count` instead reported the
        uncompressed size and overstated this store fivefold.
        """
        now = self._clock()
        orphans = 0
        total = 0
        for kind, digest, path in self._payloads.stored_files():
            if digest not in live:
                age = now - datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
                if age >= self._policy.orphan_grace:
                    self._payloads.delete(kind, digest)
                    orphans += 1
                    continue
            total += path.stat().st_size
        return orphans, total

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
            live: set[str] = {a.digest for a in artifacts if a.fetched_at >= cutoff}
            expiring = [a for a in artifacts if a.fetched_at < cutoff]
            for artifact in expiring:
                session.delete(artifact)
                removed += 1
                freed += artifact.byte_count

            # A blob can have more than one row pointing at it, so the unlink
            # is decided after every row has been judged rather than while they
            # are being judged. The store is content-addressed, which means two
            # observations that collected identical bytes *are* one file — and
            # `docs/api.md` teaches readers to expect exactly that, since equal
            # digests across two `fetched_at` values are how "nothing changed"
            # is read. Unlinking on the older row therefore took the payload of
            # a current one: a cache entry that could never be served again,
            # and a job result that raised instead of answering.
            #
            # Deduplicated because two expiring rows can share a blob too, and
            # the second unlink of one file is an error rather than a no-op.
            for kind, digest in {(a.kind, a.digest) for a in expiring if a.digest not in live}:
                self._payloads.delete(kind, digest)

        orphans, total = self._sweep_orphans(live)
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

        if orphans:
            logger.info("swept %s payload file(s) with no artifact row", orphans)

        return RetentionOutcome(
            artifacts_removed=removed,
            bytes_removed=freed,
            total_bytes=total,
            over_ceiling=over,
            orphans_removed=orphans,
        )
