"""Telling a client its job finished, instead of making it ask.

Polling works and is cheap here, so this is not a replacement for it. It exists
for the case polling serves badly: a comment harvest running for minutes, where
the alternative is holding a connection open or waking every few seconds to be
told "not yet".

Two properties make a callback safe to *receive* rather than merely convenient
to send. It is signed, so a receiver can tell our delivery from anyone who
learned the URL — and a callback URL travels in a job submission, so it is not
a secret. And the timestamp is inside the signed material rather than beside
it, so a recorded delivery cannot be replayed later with a fresh clock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from datetime import datetime

import httpx
from sqlalchemy import select

from .database import Database
from .models import Job, JobState, utcnow

logger = logging.getLogger(__name__)

TERMINAL = (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)

# Enough that a receiver restarting is not abandoned, few enough that one that
# has been down all day stops being hammered.
DEFAULT_MAXIMUM_ATTEMPTS = 8

DELIVERY_TIMEOUT_SECONDS = 10.0


def signature_for(body: bytes, *, timestamp: str, secret: str) -> str:
    """HMAC-SHA256 over timestamp and body, hex.

    The timestamp is signed with the body rather than sent alongside it. A
    signature over the body alone is replayable forever by anyone who captured
    one delivery; including the timestamp lets a receiver refuse anything older
    than its own tolerance.
    """
    material = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()


class WebhookSender:
    def __init__(
        self,
        *,
        database: Database,
        secret: str,
        maximum_attempts: int = DEFAULT_MAXIMUM_ATTEMPTS,
        clock: Callable[[], datetime] = utcnow,
        client: httpx.Client | None = None,
    ) -> None:
        self._database = database
        self._secret = secret
        self._maximum_attempts = maximum_attempts
        self._clock = clock
        self._client = client

    def deliver_pending(self) -> int:
        """Send one callback per finished job that is still owed one.

        Returns how many were accepted. A refusal leaves the job owed and
        counts the attempt, so the next sweep tries again — at-least-once in
        the sense that matters, and at-most-once on the happy path, which is
        what a receiver creating a record per callback needs.
        """
        with self._database.session(readonly=True) as session:
            owed = list(
                session.scalars(
                    select(Job).where(
                        Job.webhook_url.is_not(None),
                        Job.webhook_delivered_at.is_(None),
                        Job.state.in_(TERMINAL),
                        Job.webhook_attempts < self._maximum_attempts,
                    )
                ).all()
            )
            pending = [
                (
                    job.identifier,
                    job.webhook_url,
                    json.dumps(
                        {
                            "job_id": job.identifier,
                            "kind": job.kind,
                            "target": job.target,
                            "state": job.state.value,
                            "error_code": job.error_code,
                            "payload_bytes": job.payload_bytes,
                        }
                    ).encode(),
                )
                for job in owed
            ]

        delivered = 0
        for identifier, url, body in pending:
            if url is None:  # pragma: no cover - the query already excludes these
                continue
            if self._send(url, body):
                delivered += 1
                self._settle(identifier, succeeded=True)
            else:
                self._settle(identifier, succeeded=False)
        return delivered

    def _send(self, url: str, body: bytes) -> bool:
        timestamp = self._clock().isoformat()
        headers = {
            "Content-Type": "application/json",
            "X-Tubedepth-Timestamp": timestamp,
            "X-Tubedepth-Signature": signature_for(body, timestamp=timestamp, secret=self._secret),
        }
        client = self._client or httpx.Client(timeout=DELIVERY_TIMEOUT_SECONDS)
        try:
            response = client.post(url, content=body, headers=headers)
            return response.is_success
        except httpx.HTTPError as error:
            # A receiver being unreachable is ordinary and not our failure, so
            # it is logged at info and retried rather than raised.
            logger.info("webhook to %s could not be delivered: %s", url, error)
            return False
        finally:
            if self._client is None:
                client.close()

    def _settle(self, identifier: str, *, succeeded: bool) -> None:
        with self._database.session() as session:
            job = session.get(Job, identifier)
            if job is None:  # pragma: no cover - deleted mid-sweep
                return
            job.webhook_attempts += 1
            if succeeded:
                job.webhook_delivered_at = self._clock()
