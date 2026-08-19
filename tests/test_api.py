"""The HTTP surface, over the same services the CLI uses.

No live server and no port: httpx drives the ASGI app directly. Everything the
API does goes through CollectionService and JobService, so there is one
implementation of what a kind means rather than two.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from tubedepth.api.application import create_application
from tubedepth.database import Database
from tubedepth.egress.control import Lane
from tubedepth.egress.transport import Egress
from tubedepth.identifiers import TargetType
from tubedepth.models import Job, JobState
from tubedepth.payload_store import PayloadStore
from tubedepth.services.keys import ApiKeyService
from tubedepth.sources import SourceRegistry
from tubedepth.sources.registry import SourceCost
from tubedepth.sources.ytdlp_runtime import YtdlpRuntime


class EchoPayload(BaseModel):
    target: str


class EchoSource:
    kind = "video.echo"
    target_type = TargetType.VIDEO
    lane = Lane.YOUTUBE
    cost = SourceCost.STANDARD
    schema_version = "1"
    payload_model: type[BaseModel] = EchoPayload
    default_freshness = timedelta(hours=6)

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> EchoPayload:
        return EchoPayload(target=target)


@pytest.fixture
def api(tmp_path: Path) -> tuple[TestClient, str, Database]:
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    registry = SourceRegistry()
    registry.register(EchoSource())  # type: ignore[arg-type]
    application = create_application(
        database=database,
        payloads=PayloadStore(tmp_path / "payloads"),
        registry=registry,
    )
    minted = ApiKeyService(database).mint(label="test")
    # TestClient rather than an ASGITransport: the transport is async-only, and
    # every service under here is synchronous.
    return TestClient(application), minted.secret, database


def test_health_needs_no_key(api: tuple[TestClient, str, Database]) -> None:
    # Something has to be reachable before you have a key, or a broken deploy
    # cannot be diagnosed from outside.
    client, _, _ = api

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] in {"ok", "degraded"}


def test_a_request_without_a_key_is_refused(api: tuple[TestClient, str, Database]) -> None:
    client, _, database = api

    response = client.post("/v1/jobs", json={"kind": "video.echo", "target": "dQw4w9WgXcQ"})

    assert response.status_code == 401
    # And nothing was queued: authentication happens before any work exists.
    with database.session() as session:
        assert session.query(Job).count() == 0


def test_an_unknown_key_gets_the_same_answer_as_a_missing_one(
    api: tuple[TestClient, str, Database],
) -> None:
    # Not an oracle for whether a key exists.
    client, _, _ = api

    missing = client.get("/v1/sources")
    unknown = client.get("/v1/sources", headers={"X-API-Key": "ytd_deadbeef_nope"})

    assert missing.status_code == unknown.status_code == 401
    assert missing.json() == unknown.json()


def test_a_valid_key_reaches_the_registry(api: tuple[TestClient, str, Database]) -> None:
    client, key, _ = api

    response = client.get("/v1/sources", headers={"X-API-Key": key})

    assert response.status_code == 200
    assert "video.echo" in response.json()


def test_submitting_a_job_returns_it_with_a_location(
    api: tuple[TestClient, str, Database],
) -> None:
    client, key, _ = api

    response = client.post(
        "/v1/jobs",
        json={"kind": "video.echo", "target": "dQw4w9WgXcQ"},
        headers={"X-API-Key": key},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "queued"
    assert response.headers["location"] == f"/v1/jobs/{body['job_id']}"


def test_a_submitted_job_can_be_read_back(api: tuple[TestClient, str, Database]) -> None:
    client, key, _ = api
    job_id = client.post(
        "/v1/jobs",
        json={"kind": "video.echo", "target": "dQw4w9WgXcQ"},
        headers={"X-API-Key": key},
    ).json()["job_id"]

    response = client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": key})

    assert response.status_code == 200
    assert response.json()["kind"] == "video.echo"


def test_a_malformed_target_is_rejected_before_it_is_queued(
    api: tuple[TestClient, str, Database],
) -> None:
    client, key, database = api

    response = client.post(
        "/v1/jobs",
        json={"kind": "video.echo", "target": "not a video"},
        headers={"X-API-Key": key},
    )

    assert response.status_code == 422
    with database.session() as session:
        assert session.query(Job).count() == 0


def test_an_unknown_kind_is_a_not_found(api: tuple[TestClient, str, Database]) -> None:
    client, key, _ = api

    response = client.post(
        "/v1/jobs",
        json={"kind": "video.nonexistent", "target": "dQw4w9WgXcQ"},
        headers={"X-API-Key": key},
    )

    assert response.status_code == 404


def test_asking_for_a_result_before_the_job_finishes_says_so(
    api: tuple[TestClient, str, Database],
) -> None:
    # 409 rather than 404: the job exists, it simply has not finished, and
    # telling those apart is the difference between "wait" and "you asked for
    # something that does not exist".
    client, key, _ = api
    job_id = client.post(
        "/v1/jobs",
        json={"kind": "video.echo", "target": "dQw4w9WgXcQ"},
        headers={"X-API-Key": key},
    ).json()["job_id"]

    response = client.get(f"/v1/jobs/{job_id}/result", headers={"X-API-Key": key})

    assert response.status_code == 409


def test_a_finished_job_hands_over_its_payload(
    api: tuple[TestClient, str, Database], tmp_path: Path
) -> None:
    from tubedepth.worker import Worker

    client, key, database = api
    registry = SourceRegistry()
    registry.register(EchoSource())  # type: ignore[arg-type]
    job_id = client.post(
        "/v1/jobs",
        json={"kind": "video.echo", "target": "dQw4w9WgXcQ"},
        headers={"X-API-Key": key},
    ).json()["job_id"]

    Worker(
        database=database,
        payloads=PayloadStore(tmp_path / "payloads"),
        registry=registry,
        name="test",
        concurrency=1,
    ).drain()

    response = client.get(f"/v1/jobs/{job_id}/result", headers={"X-API-Key": key})

    assert response.status_code == 200
    assert json.loads(response.text)["target"] == "dQw4w9WgXcQ"


def test_a_job_that_does_not_exist_is_a_not_found(
    api: tuple[TestClient, str, Database],
) -> None:
    client, key, _ = api

    response = client.get("/v1/jobs/nosuchjob", headers={"X-API-Key": key})

    assert response.status_code == 404


def test_every_versioned_route_requires_a_key(
    api: tuple[TestClient, str, Database],
) -> None:
    """The mechanically-checkable version of "auth is on".

    Walks the OpenAPI document rather than app.routes: this FastAPI version
    keeps an included router as one opaque entry rather than flattening it,
    and the document is what a client actually sees anyway.

    Wiring the dependency on the router rather than per handler means a route
    added later is protected by construction; this asserts nobody has added one
    outside it.
    """
    client, _, _ = api
    paths = client.get("/openapi.json").json()["paths"]

    versioned = {path: spec for path, spec in paths.items() if path.startswith("/v1/")}
    assert versioned, "no versioned routes found to check"

    for path, spec in versioned.items():
        for method in spec:
            response = client.request(method.upper(), path.replace("{job_id}", "someid"), json={})
            assert response.status_code == 401, f"{method.upper()} {path} did not require a key"


def test_a_job_records_which_key_submitted_it(
    api: tuple[TestClient, str, Database],
) -> None:
    # How a runaway client gets identified rather than guessed at.
    client, key, database = api
    client.post(
        "/v1/jobs",
        json={"kind": "video.echo", "target": "dQw4w9WgXcQ"},
        headers={"X-API-Key": key},
    )

    with database.session() as session:
        assert session.query(Job).one().api_key_id is not None


def test_a_cached_result_comes_back_without_a_job(
    api: tuple[TestClient, str, Database], tmp_path: Path
) -> None:
    """The fast path. A fresh answer should not cost a poll cycle."""
    from tubedepth.worker import Worker

    client, key, database = api
    registry = SourceRegistry()
    registry.register(EchoSource())  # type: ignore[arg-type]
    client.post(
        "/v1/jobs",
        json={"kind": "video.echo", "target": "dQw4w9WgXcQ"},
        headers={"X-API-Key": key},
    )
    Worker(
        database=database,
        payloads=PayloadStore(tmp_path / "payloads"),
        registry=registry,
        name="test",
        concurrency=1,
    ).drain()

    response = client.post(
        "/v1/jobs",
        json={"kind": "video.echo", "target": "dQw4w9WgXcQ"},
        headers={"X-API-Key": key},
    )

    assert response.status_code == 200
    assert json.loads(response.text)["target"] == "dQw4w9WgXcQ"


def test_the_openapi_document_is_served(api: tuple[TestClient, str, Database]) -> None:
    client, _, _ = api

    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert "/v1/jobs" in response.json()["paths"]


def test_cancelling_a_queued_job_over_http_reports_it_cancelled(
    api: tuple[TestClient, str, Database],
) -> None:
    client, key, _ = api
    job_id = client.post(
        "/v1/jobs",
        headers={"X-API-Key": key},
        json={"kind": "video.echo", "target": "dQw4w9WgXcQ"},
    ).json()["job_id"]

    cancelled = client.delete(f"/v1/jobs/{job_id}", headers={"X-API-Key": key})

    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    read_back = client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": key})
    assert read_back.json()["state"] == "cancelled"


def test_cancelling_a_finished_job_is_a_conflict_and_not_a_silent_success(
    api: tuple[TestClient, str, Database],
) -> None:
    """Reporting success would tell a client it prevented work already done."""
    client, key, database = api
    job_id = client.post(
        "/v1/jobs",
        headers={"X-API-Key": key},
        json={"kind": "video.echo", "target": "dQw4w9WgXcQ"},
    ).json()["job_id"]
    with database.session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.state = JobState.FAILED
        job.error_message = "nothing came back"

    cancelled = client.delete(f"/v1/jobs/{job_id}", headers={"X-API-Key": key})

    assert cancelled.status_code == 409


def test_cancelling_needs_a_key_like_every_other_versioned_route(
    api: tuple[TestClient, str, Database],
) -> None:
    client, _, _ = api

    assert client.delete("/v1/jobs/whatever").status_code == 401
