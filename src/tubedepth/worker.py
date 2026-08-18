"""Claim jobs, run them through the registry, record what happened.

The worker knows nothing about YouTube. It knows how to take a job, ask the
registry which source serves its kind, and write down the outcome — which is
what lets a new kind of data arrive without this file changing.

Three limits compose here, in this order, and they are different limits:

  * a reserved slot for the job's cost, so a queue full of comment harvests
    cannot starve a sub-second dislike lookup;
  * a permit from the rate controller, which is the measured ceiling for that
    (egress, lane) pair rather than a number anyone chose;
  * the thread pool itself.

Threads rather than an event loop because the sources are synchronous and the
work is IO-bound: a thread per job buys the same concurrency with none of the
machinery, and lets the same source run from the CLI.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from pydantic import BaseModel

from .database import Database
from .egress.control import RateController, Verdict
from .egress.transport import DirectEgress, Egress
from .errors import RateLimitedError, TubedepthError
from .models import Job, JobState, utcnow
from .payload_store import PayloadStore
from .repositories import JobRepository
from .retrying import backoff_for_attempt, is_retryable
from .schemas import VideoListing
from .sources import SourceRegistry, default_registry
from .sources.registry import SourceCost
from .sources.ytdlp_runtime import LibraryYtdlpRuntime, YtdlpRuntime

logger = logging.getLogger(__name__)

DEFAULT_LEASE = timedelta(minutes=15)
DEFAULT_CONCURRENCY = 4

# What share of the workers each cost may hold at once. Cheap jobs are capped
# at everything because they finish in under a second; expensive ones are held
# well below the total so the cheap ones always have somewhere to run.
COST_SHARE: dict[SourceCost, float] = {
    SourceCost.CHEAP: 1.0,
    SourceCost.STANDARD: 0.75,
    SourceCost.EXPENSIVE: 0.5,
}


class Worker:
    def __init__(
        self,
        *,
        database: Database,
        payloads: PayloadStore,
        name: str,
        registry: SourceRegistry | None = None,
        runtime: YtdlpRuntime | None = None,
        egress: Egress | None = None,
        controller: RateController | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        lease: timedelta = DEFAULT_LEASE,
    ) -> None:
        self._database = database
        self._payloads = payloads
        self._name = name
        self._registry = registry or default_registry()
        self._runtime = runtime or LibraryYtdlpRuntime()
        self._egress = egress or DirectEgress()
        self._controller = controller or RateController()
        self._concurrency = max(1, concurrency)
        self._lease = lease
        self._lock = threading.Lock()
        self._in_flight_by_cost: dict[SourceCost, int] = {}

    # -- one job ---------------------------------------------------------

    def run_once(self) -> bool:
        """Take one job if there is one, and run it. Returns whether there was."""
        claimed = self._claim()
        if claimed is None:
            return False
        self._execute(*claimed)
        return True

    def reap(self) -> int:
        """Return jobs whose worker stopped reporting. Safe to call often."""
        with self._database.session() as session:
            return JobRepository(session).reap_expired_leases()

    def drain(self) -> int:
        """Run until the queue is empty. Returns how many jobs ran.

        Reaps first: a previous run killed mid-job left rows in `running` that
        nothing else will ever release, and starting without collecting them
        means the queue looks shorter than it is.
        """
        reaped = self.reap()
        if reaped:
            logger.info("returned %s job(s) whose lease had expired", reaped)

        if self._concurrency == 1:
            completed = 0
            while self.run_once():
                completed += 1
            return completed

        completed = 0
        counted = threading.Lock()

        def pump() -> None:
            nonlocal completed
            while True:
                claimed = self._claim()
                if claimed is None:
                    return
                self._execute(*claimed)
                with counted:
                    completed += 1

        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            for future in [pool.submit(pump) for _ in range(self._concurrency)]:
                future.result()
        return completed

    # -- the gates -------------------------------------------------------

    def _claim(self) -> tuple[str, str, str, str | None] | None:
        """Take a job and reserve its cost slot, atomically.

        Both under one lock because checking the reservation and then taking it
        is a race: two threads can each read two expensive jobs in flight
        against a cap of two and both proceed, which makes the reservation
        advisory rather than enforced — and it shows up only under load, which
        is the only time it matters. Serialising the claim costs nothing:
        SQLite serialises writers regardless.
        """
        with self._lock:
            kinds = self._admissible_kinds_unlocked()
            if kinds is not None and not kinds:
                return None
            with self._database.session() as session:
                job = JobRepository(session).claim(
                    worker=self._name, lease=self._lease, kinds=kinds
                )
                if job is None:
                    return None
                claimed = (job.identifier, job.kind, job.target, job.follow_up_kind)
                cost = self._registry.get(job.kind).cost if self._knows(job.kind) else None
            if cost is not None:
                self._in_flight_by_cost[cost] = self._in_flight_by_cost.get(cost, 0) + 1
            return claimed

    def _knows(self, kind: str) -> bool:
        try:
            self._registry.get(kind)
        except TubedepthError:
            return False
        return True

    def _admissible_kinds_unlocked(self) -> list[str] | None:
        """Which kinds this worker may take right now. The caller holds the lock.

        Costs already at their reserved share are excluded from the claim query
        itself, rather than claimed and then put back — a job returned to the
        queue has burned an attempt and lost its place.
        """
        allowed = {
            cost
            for cost, share in COST_SHARE.items()
            if self._in_flight_by_cost.get(cost, 0) < max(1, int(self._concurrency * share))
        }
        if len(allowed) == len(COST_SHARE):
            return None
        return [kind for kind in self._registry.kinds() if self._registry.get(kind).cost in allowed]

    def _execute(self, identifier: str, kind: str, target: str, follow_up: str | None) -> None:
        try:
            source = self._registry.get(kind)
        except TubedepthError as error:  # noqa: BLE001 - reported on the row below
            # A job naming a kind this build does not have. Fails on the row
            # rather than escaping into the pump loop and stopping the worker.
            logger.warning("job %s names an unknown kind: %s", identifier, error)
            self._settle(identifier, JobState.FAILED, error=error)
            return

        try:
            if not self._wait_for_permit(source.lane):
                # Nothing available and the route is quarantined. Put it back
                # rather than failing it: the job is fine, the route is not.
                self._requeue(identifier)
                return
            try:
                result, digest, byte_count = self._collect(kind, target)
            except TubedepthError as error:
                verdict = (
                    Verdict.BLOCKED if isinstance(error, RateLimitedError) else Verdict.THROTTLED
                )
                self._controller.release(self._egress.name, source.lane, verdict)
                self._fail_or_retry(identifier, kind, error)
                return
            self._controller.release(self._egress.name, source.lane, Verdict.OK)
        finally:
            self._leave(source.cost)

        logger.info("job %s (%s) collected %s bytes", identifier, kind, byte_count)
        self._settle(identifier, JobState.SUCCEEDED, digest=digest, byte_count=byte_count)

        if follow_up is not None and isinstance(result, VideoListing):
            queued = self._queue_follow_up(result, follow_up)
            logger.info("job %s queued %s follow-up %s jobs", identifier, queued, follow_up)

    def _wait_for_permit(self, lane: object) -> bool:
        """Block until the controller allows one more request on this lane."""
        deadline = self._lease.total_seconds()
        waited = 0.0
        while waited < deadline:
            if self._controller.acquire(self._egress.name, lane):  # type: ignore[arg-type]
                return True
            if not self._controller.is_available(self._egress.name, lane):  # type: ignore[arg-type]
                return False
            threading.Event().wait(0.05)
            waited += 0.05
        return False

    def _leave(self, cost: SourceCost) -> None:
        with self._lock:
            self._in_flight_by_cost[cost] = max(0, self._in_flight_by_cost.get(cost, 0) - 1)

    # -- persistence -----------------------------------------------------

    def _collect(self, kind: str, target: str) -> tuple[BaseModel, str, int]:
        source = self._registry.get(kind)
        result = source.collect(target, self._egress, self._runtime)
        stored = self._payloads.put(kind, result.model_dump_json(indent=1).encode())
        return result, stored.digest, stored.byte_count

    def _queue_follow_up(self, listing: VideoListing, kind: str) -> int:
        """Turn a listing into work.

        The follow-up kind is validated against the registry first, so a typo
        costs nothing rather than queueing a hundred jobs that can only fail.
        """
        self._registry.get(kind)
        with self._database.session() as session:
            for video in listing.videos:
                session.add(Job(kind=kind, target=video.video_id))
        return len(listing.videos)

    def _fail_or_retry(self, identifier: str, kind: str, error: TubedepthError) -> None:
        """Give the job another go, or stop and say why.

        Whether a failure is worth retrying is decided by the error class, so
        the worker cannot get it wrong and the answer does not depend on which
        boundary caught it.
        """
        with self._database.session() as session:
            job = session.get(Job, identifier)
            if job is None:  # pragma: no cover - the row was deleted mid-flight
                return
            exhausted = job.attempt_count >= job.max_attempts
            if not is_retryable(error) or exhausted:
                reason = "exhausted its attempts" if exhausted else "is not retryable"
                logger.warning("job %s (%s) failed, %s: %s", identifier, kind, reason, error)
                job.state = JobState.FAILED
                job.finished_at = utcnow()
                job.error_code = type(error).__name__
                job.error_message = str(error)
                return

            delay = backoff_for_attempt(job.attempt_count)
            job.state = JobState.QUEUED
            job.claimed_by = None
            job.lease_expires_at = None
            # Enforced by the claim, which filters on scheduled_at, rather than
            # merely recorded — a worker that picks the job straight back up
            # has waited nothing at all.
            job.scheduled_at = utcnow() + delay
            job.error_code = type(error).__name__
            job.error_message = str(error)
            logger.info(
                "job %s (%s) attempt %s failed, retrying in %.0fs: %s",
                identifier,
                kind,
                job.attempt_count,
                delay.total_seconds(),
                error,
            )

    def _requeue(self, identifier: str) -> None:
        with self._database.session() as session:
            job = session.get(Job, identifier)
            if job is not None:
                job.state = JobState.QUEUED
                job.claimed_by = None
                job.lease_expires_at = None

    def _settle(
        self,
        identifier: str,
        state: JobState,
        *,
        digest: str | None = None,
        byte_count: int | None = None,
        error: TubedepthError | None = None,
    ) -> None:
        with self._database.session() as session:
            job = session.get(Job, identifier)
            if job is None:  # pragma: no cover - the row was deleted mid-flight
                return
            job.state = state
            job.finished_at = utcnow()
            job.payload_digest = digest
            job.payload_bytes = byte_count
            if error is not None:
                job.error_code = type(error).__name__
                job.error_message = str(error)
