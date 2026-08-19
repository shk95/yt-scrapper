"""What each source has been doing lately, kept where the API can read it.

The rate controller already knows when a *route* is in trouble, but that lives
in the worker's memory and dies with the process — so nothing outside the
worker can see it. This is the other half and a different question: not "may I
make another request" but "is this kind of collection still working at all".

Per source rather than per lane, because that is the question that goes
unanswered today. When YouTube renames a renderer, `video.related` starts
failing every time while `video.metadata` beside it is fine; the lane is
healthy and the source is not.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from .database import Database
from .egress.control import LaneState
from .errors import ExtractionError, RateLimitedError, UpstreamError
from .models import LaneHealth, SourceHealth, utcnow

# How many failures in a row before a source is called broken rather than
# unlucky. Three, because one is noise and two is a coincidence — and because
# a renamed renderer fails every single call, so three arrives within seconds.
BROKEN_AFTER = 3

# A source last seen working longer ago than this is reported as stale rather
# than healthy. Green-because-nobody-asked is how a dashboard lies.
DEFAULT_STALE_AFTER = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class HealthEntry:
    kind: str
    status: str
    consecutive_failures: int
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error_code: str | None


def _counts_against_the_source(error: BaseException | None) -> bool:
    """Whether a failure says anything about the source's health.

    The same distinction the rate controller needed, in a second place and for
    a second reason. A video with captions turned off makes `video.transcript`
    fail legitimately and repeatedly; a sweep of such videos would paint the
    source red while it works perfectly.

    So only two kinds of failure count. `ExtractionError` means our parser no
    longer matches what YouTube sends, which is the exact thing this table
    exists to surface. `UpstreamError` and its subclasses mean the other end
    refused or could not be reached. Everything else — a missing caption track,
    a private video, a bad identifier — is a fact about the target.
    """
    return isinstance(error, ExtractionError | UpstreamError)


class SourceHealthService:
    def __init__(
        self,
        *,
        database: Database,
        clock: Callable[[], datetime] = utcnow,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
    ) -> None:
        self._database = database
        self._clock = clock
        self._stale_after = stale_after

    def record(self, kind: str, *, succeeded: bool, error: BaseException | None = None) -> None:
        if not succeeded and not _counts_against_the_source(error):
            return

        now = self._clock()
        with self._database.session() as session:
            # Explicit rather than leaning on the column default: that applies
            # at flush, so a freshly constructed row still holds None and the
            # increment below would raise on the first failure a source ever has.
            row = session.get(SourceHealth, kind) or SourceHealth(
                kind=kind, consecutive_failures=0, blocked=False
            )
            if succeeded:
                row.last_success_at = now
                row.consecutive_failures = 0
                row.last_error_code = None
            else:
                row.last_failure_at = now
                row.consecutive_failures += 1
                row.last_error_code = type(error).__name__
                row.last_error_message = str(error) if error else None
                row.blocked = isinstance(error, RateLimitedError)
            session.add(row)

    def snapshot(self) -> dict[str, HealthEntry]:
        """Every registered source, including those never tried.

        Absent rows are reported as `unknown` rather than omitted: a dashboard
        that shows nothing for a source nobody has run cannot be told apart
        from one where the source does not exist.

        The union of registered kinds and recorded ones, so a kind dropped from
        the build does not take its history off the screen without explanation.
        """
        from .sources import default_registry

        now = self._clock()
        with self._database.session(readonly=True) as session:
            rows = {row.kind: row for row in session.scalars(select(SourceHealth)).all()}

        entries: dict[str, HealthEntry] = {}
        for kind in sorted({*default_registry().kinds(), *rows}):
            row = rows.get(kind)
            entries[kind] = HealthEntry(
                kind=kind,
                status=self._status(row, now),
                consecutive_failures=row.consecutive_failures if row else 0,
                last_success_at=row.last_success_at if row else None,
                last_failure_at=row.last_failure_at if row else None,
                last_error_code=row.last_error_code if row else None,
            )
        return entries

    def _status(self, row: SourceHealth | None, now: datetime) -> str:
        if row is None or (row.last_success_at is None and row.consecutive_failures == 0):
            return "unknown"
        if row.consecutive_failures >= BROKEN_AFTER:
            return "blocked" if row.blocked else "broken"
        if row.consecutive_failures:
            return "degraded"
        if row.last_success_at is not None and now - row.last_success_at > self._stale_after:
            return "stale"
        return "healthy"


class LaneHealthService:
    """Write what the rate controller believes somewhere another process can read.

    The controller keeps its state in a dict keyed by `(egress, lane)` and
    measures time with `time.monotonic`, both of which are correct and both of
    which are invisible outside the worker. So a quarantined lane looks exactly
    like an empty queue from the API, from the dashboard, and from anyone
    trying to work out why collection stopped.

    Written on the same tick as source health, for the same reason: the process
    that knows is not the process being asked.
    """

    def __init__(self, *, database: Database, clock: Callable[[], datetime] = utcnow) -> None:
        self._database = database
        self._clock = clock

    def observe(self, egress: str, lane: str, *, state: LaneState, monotonic: float) -> None:
        """Record one lane's state, converting its deadline to a wall clock.

        `monotonic` is the controller's own reading taken at the same moment,
        so the remaining quarantine is a difference between two monotonic
        values — which is the only arithmetic on them that is meaningful — and
        only the result crosses into wall-clock time.
        """
        now = self._clock()
        remaining = state.quarantined_until - monotonic
        with self._database.session() as session:
            row = session.get(LaneHealth, (egress, lane)) or LaneHealth(egress=egress, lane=lane)
            row.window = state.window
            row.in_flight = state.in_flight
            row.quarantine_streak = state.quarantine_streak
            row.quarantined_until = now + timedelta(seconds=remaining) if remaining > 0 else None
            row.observed_at = now
            session.add(row)
