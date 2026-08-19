"""Telling a client its job finished, instead of making it ask.

Polling works and costs little here, so this exists for the case polling
cannot serve: a comment harvest that runs for minutes, where the client would
otherwise hold a connection or wake up every few seconds to be told "not yet".

Two things make a webhook safe to receive rather than merely convenient to
send. It is signed, so the receiver can tell our callback from anyone who
learned the URL. And it is timestamped inside the signed material, so a
recorded delivery cannot be replayed at leisure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from tubedepth.database import Database
from tubedepth.models import Job, JobState
from tubedepth.webhooks import WebhookSender, signature_for


def job_row(database: Database, **fields: object) -> str:
    with database.session() as session:
        job = Job(kind="video.metadata", target="dQw4w9WgXcQ", **fields)
        session.add(job)
        session.flush()
        return job.identifier


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    return database


@respx.mock
def test_a_finished_job_is_announced_to_its_callback(database: Database) -> None:
    route = respx.post("https://example.invalid/hook").respond(200)
    identifier = job_row(
        database,
        state=JobState.SUCCEEDED,
        webhook_url="https://example.invalid/hook",
        payload_bytes=1234,
    )

    delivered = WebhookSender(database=database, secret="shh").deliver_pending()

    assert delivered == 1
    assert route.called
    body = route.calls[0].request.content.decode()
    assert identifier in body
    assert "succeeded" in body


@respx.mock
def test_the_delivery_is_signed_over_body_and_timestamp(database: Database) -> None:
    """Unsigned, a callback is an invitation to whoever learns the URL.

    The timestamp is inside the signed material rather than beside it, so a
    recorded delivery cannot be replayed later with its own clock.
    """
    route = respx.post("https://example.invalid/hook").respond(200)
    job_row(database, state=JobState.SUCCEEDED, webhook_url="https://example.invalid/hook")

    WebhookSender(database=database, secret="shh").deliver_pending()

    request = route.calls[0].request
    timestamp = request.headers["X-Tubedepth-Timestamp"]
    expected = signature_for(request.content, timestamp=timestamp, secret="shh")
    assert request.headers["X-Tubedepth-Signature"] == expected
    assert signature_for(request.content, timestamp=timestamp, secret="different") != expected


@respx.mock
def test_a_job_still_running_is_not_announced(database: Database) -> None:
    route = respx.post("https://example.invalid/hook").respond(200)
    job_row(database, state=JobState.RUNNING, webhook_url="https://example.invalid/hook")

    assert WebhookSender(database=database, secret="shh").deliver_pending() == 0
    assert not route.called


@respx.mock
def test_a_delivered_callback_is_never_sent_twice(database: Database) -> None:
    """At-most-once on the happy path. A receiver that creates a record per
    callback should not get two for one job."""
    respx.post("https://example.invalid/hook").respond(200)
    job_row(database, state=JobState.SUCCEEDED, webhook_url="https://example.invalid/hook")
    sender = WebhookSender(database=database, secret="shh")

    assert sender.deliver_pending() == 1
    assert sender.deliver_pending() == 0


@respx.mock
def test_a_refused_delivery_is_retried_rather_than_dropped(database: Database) -> None:
    respx.post("https://example.invalid/hook").mock(
        side_effect=[httpx.Response(503), httpx.Response(200)]
    )
    job_row(database, state=JobState.SUCCEEDED, webhook_url="https://example.invalid/hook")
    sender = WebhookSender(database=database, secret="shh")

    assert sender.deliver_pending() == 0
    assert sender.deliver_pending() == 1


@respx.mock
def test_a_callback_that_keeps_refusing_is_eventually_abandoned(database: Database) -> None:
    """A receiver that has been down for a day must not hold the queue open
    forever, and must not be hammered indefinitely either."""
    respx.post("https://example.invalid/hook").respond(500)
    identifier = job_row(
        database, state=JobState.SUCCEEDED, webhook_url="https://example.invalid/hook"
    )
    sender = WebhookSender(database=database, secret="shh", maximum_attempts=3)

    for _ in range(5):
        sender.deliver_pending()

    with database.session(readonly=True) as session:
        job = session.get(Job, identifier)
        assert job is not None
        assert job.webhook_attempts == 3, "kept trying past the limit"
        assert job.webhook_delivered_at is None


def test_the_signature_is_stable_for_the_same_input() -> None:
    body = b'{"job_id": "x"}'
    stamp = datetime(2026, 8, 19, tzinfo=UTC).isoformat()

    assert signature_for(body, timestamp=stamp, secret="s") == signature_for(
        body, timestamp=stamp, secret="s"
    )
    assert signature_for(body, timestamp=stamp, secret="s") != signature_for(
        b'{"job_id": "y"}', timestamp=stamp, secret="s"
    )


@respx.mock
def test_a_submission_can_ask_to_be_called_back(tmp_path: Path) -> None:
    """The URL travels with the job, so a submitter chooses per request rather
    than the operator choosing once for everyone."""
    from test_api import build_api

    client, key, database = build_api(tmp_path)
    response = client.post(
        "/v1/jobs",
        headers={"X-API-Key": key},
        json={
            "kind": "video.echo",
            "target": "dQw4w9WgXcQ",
            "webhook_url": "https://example.invalid/hook",
        },
    )

    assert response.status_code in (200, 202)
    with database.session(readonly=True) as session:
        job = session.get(Job, response.json()["job_id"])
        assert job is not None
        assert job.webhook_url == "https://example.invalid/hook"


def test_a_webhook_url_that_is_not_a_url_is_refused(tmp_path: Path) -> None:
    """A malformed callback stored is a delivery that fails forever."""
    from test_api import build_api

    client, key, _ = build_api(tmp_path)

    response = client.post(
        "/v1/jobs",
        headers={"X-API-Key": key},
        json={"kind": "video.echo", "target": "dQw4w9WgXcQ", "webhook_url": "not-a-url"},
    )

    assert response.status_code == 422


@respx.mock
def test_the_worker_delivers_callbacks_as_it_finishes_jobs(tmp_path: Path) -> None:
    """The check that `renew_lease` did not have.

    A sender nothing calls is a feature that exists in the tests and not in the
    system, and this repository has already shipped one of those.
    """
    from test_worker import EchoSource, _registry

    from tubedepth.payload_store import PayloadStore
    from tubedepth.worker import Worker

    route = respx.post("https://example.invalid/hook").respond(200)
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    with database.session() as session:
        session.add(
            Job(
                kind="video.echo",
                target="video000001",
                webhook_url="https://example.invalid/hook",
            )
        )

    Worker(
        database=database,
        registry=_registry(EchoSource()),
        payloads=PayloadStore(tmp_path / "payloads"),
        name="worker-1",
        webhook_secret="shh",
    ).drain()

    assert route.called, "the job finished and its callback was never sent"


def test_a_worker_with_no_secret_configured_sends_nothing(tmp_path: Path) -> None:
    """Unsigned deliveries are worse than none: a receiver cannot tell them
    from anyone else who learned the URL, so silence is the safer default."""
    from test_worker import EchoSource, _registry

    from tubedepth.payload_store import PayloadStore
    from tubedepth.worker import Worker

    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    worker = Worker(
        database=database,
        registry=_registry(EchoSource()),
        payloads=PayloadStore(tmp_path / "payloads"),
        name="worker-1",
    )

    assert worker.deliver_webhooks() == 0


@respx.mock
def test_taking_one_job_and_stopping_still_delivers_its_callback(tmp_path: Path) -> None:
    """`--once` is the invocation with no next run to catch up.

    `drain` says so itself — it delivers on exit "so jobs this run finished are
    announced without waiting for the next one, which for a `--once`
    invocation would be never" — and then `--once` did not go through `drain`.
    The mitigation written for this case was in the method this case does not
    call, which is the shape `decisions/003` is about.
    """
    from test_worker import EchoSource, _registry

    from tubedepth.payload_store import PayloadStore
    from tubedepth.worker import Worker

    route = respx.post("https://example.invalid/hook").respond(200)
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    with database.session() as session:
        session.add(
            Job(
                kind="video.echo",
                target="video000001",
                webhook_url="https://example.invalid/hook",
            )
        )

    completed = Worker(
        database=database,
        registry=_registry(EchoSource()),
        payloads=PayloadStore(tmp_path / "payloads"),
        name="worker-1",
        webhook_secret="shh",
    ).drain(limit=1)

    assert completed == 1
    assert route.called, "a job finished by --once was never announced"
