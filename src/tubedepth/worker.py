"""Claim a job, run it through the registry, record what happened.

The worker knows nothing about YouTube. It knows how to take a job, ask the
registry which source serves its kind, and write down the outcome — which is
what lets a new kind of data arrive without this file changing.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from pydantic import BaseModel

from .database import Database
from .egress.transport import DirectEgress, Egress
from .errors import TubedepthError
from .models import Job, JobState, utcnow
from .payload_store import PayloadStore
from .repositories import JobRepository
from .schemas import VideoListing
from .sources import SourceRegistry, default_registry
from .sources.ytdlp_runtime import LibraryYtdlpRuntime, YtdlpRuntime

logger = logging.getLogger(__name__)

DEFAULT_LEASE = timedelta(minutes=15)


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
        lease: timedelta = DEFAULT_LEASE,
    ) -> None:
        self._database = database
        self._payloads = payloads
        self._name = name
        self._registry = registry or default_registry()
        self._runtime = runtime or LibraryYtdlpRuntime()
        self._egress = egress or DirectEgress()
        self._lease = lease

    def run_once(self) -> bool:
        """Take one job if there is one. Returns whether there was."""
        with self._database.session() as session:
            job = JobRepository(session).claim(worker=self._name, lease=self._lease)
            if job is None:
                return False
            identifier, kind, target = job.identifier, job.kind, job.target
            follow_up = job.follow_up_kind

        try:
            result, digest, byte_count = self._collect(kind, target)
        except TubedepthError as error:
            logger.warning("job %s (%s) failed: %s", identifier, kind, error)
            self._settle(identifier, JobState.FAILED, error=error)
            return True

        logger.info("job %s (%s) collected %s bytes", identifier, kind, byte_count)
        self._settle(identifier, JobState.SUCCEEDED, digest=digest, byte_count=byte_count)

        if follow_up is not None and isinstance(result, VideoListing):
            queued = self._queue_follow_up(result, follow_up)
            logger.info("job %s queued %s follow-up %s jobs", identifier, queued, follow_up)
        return True

    def drain(self) -> int:
        """Run until the queue is empty. Returns how many jobs ran."""
        completed = 0
        while self.run_once():
            completed += 1
        return completed

    def _collect(self, kind: str, target: str) -> tuple[BaseModel, str, int]:
        source = self._registry.get(kind)
        result = source.collect(target, self._egress, self._runtime)
        stored = self._payloads.put(kind, result.model_dump_json(indent=1).encode())
        return result, stored.digest, stored.byte_count

    def _queue_follow_up(self, listing: VideoListing, kind: str) -> int:
        """Turn a listing into work.

        The link that makes volume possible: without it a listing is a report
        someone has to read and retype. The follow-up kind is validated against
        the registry first, so a typo costs nothing rather than queueing a
        hundred jobs that can only fail.
        """
        self._registry.get(kind)
        with self._database.session() as session:
            for video in listing.videos:
                session.add(Job(kind=kind, target=video.video_id))
        return len(listing.videos)

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
