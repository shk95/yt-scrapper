"""The HTTP surface, over the same services the CLI uses.

No live server and no port: httpx drives the ASGI app directly. Everything the
API does goes through CollectionService and JobService, so there is one
implementation of what a kind means rather than two.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from tubedepth.api.application import MAXIMUM_PAGE, create_application
from tubedepth.database import Database
from tubedepth.egress.control import Lane
from tubedepth.egress.transport import Egress
from tubedepth.errors import ConfigurationError, TubedepthError, UnavailableError
from tubedepth.identifiers import TargetType
from tubedepth.models import Artifact, Job, JobState
from tubedepth.payload_store import PayloadStore
from tubedepth.services.keys import ApiKeyService
from tubedepth.sources import SourceRegistry
from tubedepth.sources.registry import DataSource, SourceCost
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


def build_api(tmp_path: Path, database: Database) -> tuple[TestClient, str, Database]:
    """The fixture's body as a plain function.

    Other test modules need the same client and were reaching into the
    fixture's `__wrapped__`, which is an implementation detail of pytest and
    not a seam anyone should rely on.
    """
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


@pytest.fixture
def api(tmp_path: Path, database: Database) -> tuple[TestClient, str, Database]:
    return build_api(tmp_path, database)


class RaisingRegistry(SourceRegistry):
    """A registry whose lookup fails with whichever domain error it was given.

    `registry.get` is the first thing `POST /v1/jobs` touches, so this raises
    the failure from inside a real route — which is where the status mapping
    is attached — rather than by calling the handler directly. Nothing else
    provokes an `UnavailableError` or a `ConfigurationError` without a network
    or a database that is misconfigured on purpose.
    """

    def __init__(self, failure: TubedepthError) -> None:
        super().__init__()
        self._failure = failure

    def get(self, kind: str) -> DataSource:
        raise self._failure


def build_api_that_fails_with(
    tmp_path: Path, database: Database, failure: TubedepthError
) -> tuple[TestClient, str]:
    application = create_application(
        database=database,
        payloads=PayloadStore(tmp_path / "payloads"),
        registry=RaisingRegistry(failure),
    )
    return TestClient(application), ApiKeyService(database).mint(label="test").secret


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


def test_a_query_value_that_cannot_be_parsed_comes_back_in_the_documented_shape(
    api: tuple[TestClient, str, Database],
) -> None:
    """`docs/api.md` promises one error shape and this used to be the other.

    Everything raised inside a route reaches the domain handler and answers
    `{"error": {...}}`; everything FastAPI refuses before the route runs used
    to answer `{"detail": [...]}`. Two shapes for one class of failure is two
    branches in every client, for the failures a client provokes most often.
    """
    client, key, _ = api

    response = client.get("/v1/jobs?since=last-tuesday", headers={"X-API-Key": key})

    assert response.status_code == 422
    body = response.json()
    assert "detail" not in body, "FastAPI's own shape reached a client"
    assert body["error"]["code"] == "invalid_request"
    assert "since" in body["error"]["message"], "the message says which value was refused"


def test_a_malformed_body_comes_back_in_the_documented_shape(
    api: tuple[TestClient, str, Database],
) -> None:
    client, key, _ = api

    response = client.post("/v1/jobs", json={"kind": "video.echo"}, headers={"X-API-Key": key})

    assert response.status_code == 422
    body = response.json()
    assert "detail" not in body
    assert body["error"]["code"] == "invalid_request"
    assert "target" in body["error"]["message"], "the message names the missing field"


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code"),
    [
        (UnavailableError("video is not available in this country"), 404, "unavailable"),
        (ConfigurationError("TUBEDEPTH_DATA_API_KEY is not set"), 503, "not_configured"),
    ],
    ids=["unavailable", "not_configured"],
)
def test_a_failure_that_is_not_our_bug_is_not_reported_as_our_bug(
    tmp_path: Path,
    database: Database,
    failure: TubedepthError,
    expected_status: int,
    expected_code: str,
) -> None:
    """Both used to fall through to the catch-all as 500 `internal_error`.

    Which the reference defines as our bug — so a geo-blocked video and an
    unset key both sent whoever was on call into our tracebacks. Since #16 a
    `search_path` that does not lead with this project's schema raises
    `ConfigurationError` too, and 503 is what tells an operator to go and look
    at the configuration.
    """
    client, key = build_api_that_fails_with(tmp_path, database, failure)

    response = client.post(
        "/v1/jobs",
        json={"kind": "video.echo", "target": "dQw4w9WgXcQ"},
        headers={"X-API-Key": key},
    )

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code


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


def test_a_forced_collection_records_a_second_observation(
    api: tuple[TestClient, str, Database], tmp_path: Path
) -> None:
    """`refresh` has to survive the queue, not just the request handler.

    The artifact table is the history this project keeps by appending, so a
    forced collection that is quietly served from the cache records no new row:
    the series stops moving while every job still reports success and points at
    a digest. Nothing errors, which is the failure this repository exists to
    make impossible — and it is the one the trend work depends on not having.
    """
    from tubedepth.worker import Worker

    client, key, database = api

    def drain() -> None:
        registry = SourceRegistry()
        registry.register(EchoSource())  # type: ignore[arg-type]
        Worker(
            database=database,
            payloads=PayloadStore(tmp_path / "payloads"),
            registry=registry,
            name="test",
            concurrency=1,
        ).drain()

    client.post(
        "/v1/jobs",
        json={"kind": "video.echo", "target": "dQw4w9WgXcQ"},
        headers={"X-API-Key": key},
    )
    drain()

    response = client.post(
        "/v1/jobs",
        json={"kind": "video.echo", "target": "dQw4w9WgXcQ", "refresh": True},
        headers={"X-API-Key": key},
    )
    drain()

    assert response.status_code == 202, "a forced collection answered with the cached body"
    with database.session() as session:
        assert session.query(Artifact).count() == 2, (
            "the forced collection was served from the cache and recorded no observation"
        )


def test_a_job_whose_payload_has_aged_out_says_so_instead_of_raising(
    api: tuple[TestClient, str, Database], tmp_path: Path
) -> None:
    """Retention deletes artifacts and never touches job rows.

    So this is the ordinary state of every job older than the retention
    window, not a corner case: the row still names a digest and the bytes are
    gone. `payloads.read` raises `FileNotFoundError`, which is not a
    `TubedepthError` and therefore reaches FastAPI's default handler — an
    unhandled traceback and a 500 for the most predictable outcome this
    endpoint has. A 500 sends whoever is on call into our tracebacks to
    discover that retention did exactly what it was configured to do.
    """
    from tubedepth.worker import Worker

    client, key, database = api
    registry = SourceRegistry()
    registry.register(EchoSource())  # type: ignore[arg-type]
    job_id = client.post(
        "/v1/jobs",
        json={"kind": "video.echo", "target": "dQw4w9WgXcQ"},
        headers={"X-API-Key": key},
    ).json()["job_id"]
    payloads = PayloadStore(tmp_path / "payloads")
    Worker(
        database=database, payloads=payloads, registry=registry, name="test", concurrency=1
    ).drain()
    with database.session() as session:
        job = session.get(Job, job_id)
        assert job is not None and job.payload_digest is not None
        digest = job.payload_digest
    # What retention does, without waiting thirty days for it.
    payloads.delete("video.echo", digest)

    response = client.get(f"/v1/jobs/{job_id}/result", headers={"X-API-Key": key})

    assert response.status_code == 404, "an aged-out payload answered as though it were our bug"
    assert response.json()["error"]["code"] == "not_found"


def test_the_openapi_document_is_served(api: tuple[TestClient, str, Database]) -> None:
    client, _, _ = api

    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert "/v1/jobs" in response.json()["paths"]


def test_the_openapi_document_advertises_the_page_bounds(
    api: tuple[TestClient, str, Database],
) -> None:
    """The advertised contract moved with the behaviour, which is the point.

    `limit` was a bare `int` with the bound applied afterwards by a clamp, so
    the schema promised an unbounded integer and the code quietly refused to
    honour it. A generated client reading that schema would have offered a
    caller a page size the API never intended to serve.
    """
    client, _, _ = api

    paths = client.get("/openapi.json").json()["paths"]

    for route in ("/v1/jobs", "/v1/artifacts"):
        declared = next(
            parameter
            for parameter in paths[route]["get"]["parameters"]
            if parameter["name"] == "limit"
        )
        assert declared["schema"]["minimum"] == 1
        assert declared["schema"]["maximum"] == MAXIMUM_PAGE


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


def test_health_reports_every_source_and_not_just_the_queue(
    api: tuple[TestClient, str, Database],
) -> None:
    """What an operator asking "why is nothing arriving" needs to see.

    Queue depth answers "is there work"; it cannot answer "is anything still
    able to do it". A source that has started failing every call looks
    identical to an idle system from the counts alone.
    """
    client, key, _ = api

    body = client.get("/healthz").json()

    assert "sources" in body, "the queue counts alone cannot say what is broken"
    assert body["sources"], "every registered source is reported, including untried ones"
    statuses = {entry["status"] for entry in body["sources"]}
    assert statuses <= {"unknown", "healthy", "degraded", "broken", "blocked", "stale"}


def test_health_names_the_broken_source_and_its_last_error(
    api: tuple[TestClient, str, Database],
) -> None:
    client, key, database = api
    from tubedepth.errors import ExtractionError
    from tubedepth.health import SourceHealthService

    health = SourceHealthService(database=database)
    for _ in range(3):
        health.record("video.echo", succeeded=False, error=ExtractionError("no renderer named x"))

    entries = {entry["kind"]: entry for entry in client.get("/healthz").json()["sources"]}

    assert entries["video.echo"]["status"] == "broken"
    assert entries["video.echo"]["last_error_code"] == "ExtractionError"
    assert entries["video.echo"]["consecutive_failures"] == 3


def test_an_unhealthy_source_does_not_make_the_whole_service_unhealthy(
    api: tuple[TestClient, str, Database],
) -> None:
    """`/healthz` is read by things that restart processes.

    One broken parser is not a reason to cycle the API — the other nine kinds
    are still collecting. The status stays `ok` and the detail carries the bad
    news.
    """
    client, _, database = api
    from tubedepth.errors import ExtractionError
    from tubedepth.health import SourceHealthService

    health = SourceHealthService(database=database)
    for _ in range(5):
        health.record("video.echo", succeeded=False, error=ExtractionError("gone"))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_jobs_can_be_listed_newest_first(api: tuple[TestClient, str, Database]) -> None:
    client, key, _ = api
    for index in range(3):
        client.post(
            "/v1/jobs",
            headers={"X-API-Key": key},
            json={"kind": "video.echo", "target": f"vid{index:08d}"},
        )

    listed = client.get("/v1/jobs", headers={"X-API-Key": key}).json()

    assert len(listed["jobs"]) == 3
    assert listed["jobs"][0]["target"] == "vid00000002", "newest first"
    assert "cursor" in listed


def test_jobs_can_be_filtered_by_state_and_kind(api: tuple[TestClient, str, Database]) -> None:
    client, key, database = api
    first = client.post(
        "/v1/jobs",
        headers={"X-API-Key": key},
        json={"kind": "video.echo", "target": "vid00000001"},
    ).json()["job_id"]
    client.post(
        "/v1/jobs",
        headers={"X-API-Key": key},
        json={"kind": "video.echo", "target": "vid00000002"},
    )
    with database.session() as session:
        job = session.get(Job, first)
        assert job is not None
        job.state = JobState.FAILED
        job.error_code = "ExtractionError"

    failed = client.get("/v1/jobs?state=failed", headers={"X-API-Key": key}).json()

    assert [job["target"] for job in failed["jobs"]] == ["vid00000001"]
    assert failed["jobs"][0]["error_code"] == "ExtractionError"


def test_jobs_can_be_narrowed_to_a_time_range(api: tuple[TestClient, str, Database]) -> None:
    """The range selection this exists for — 'show me what ran last night'."""
    client, key, database = api
    job_id = client.post(
        "/v1/jobs",
        headers={"X-API-Key": key},
        json={"kind": "video.echo", "target": "vid00000001"},
    ).json()["job_id"]
    with database.session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.created_at = datetime(2026, 1, 1, tzinfo=UTC)

    within = client.get(
        "/v1/jobs?since=2025-12-31T00:00:00Z&until=2026-01-02T00:00:00Z", headers={"X-API-Key": key}
    ).json()
    outside = client.get("/v1/jobs?since=2026-06-01T00:00:00Z", headers={"X-API-Key": key}).json()

    assert len(within["jobs"]) == 1
    assert outside["jobs"] == []


def test_the_job_list_pages_rather_than_returning_everything(
    api: tuple[TestClient, str, Database],
) -> None:
    """A browser pointed at a hundred thousand rows must not fetch them."""
    client, key, _ = api
    for index in range(5):
        client.post(
            "/v1/jobs",
            headers={"X-API-Key": key},
            json={"kind": "video.echo", "target": f"vid{index:08d}"},
        )

    first = client.get("/v1/jobs?limit=2", headers={"X-API-Key": key}).json()
    second = client.get(
        f"/v1/jobs?limit=2&cursor={first['cursor']}", headers={"X-API-Key": key}
    ).json()

    assert len(first["jobs"]) == 2
    assert len(second["jobs"]) == 2
    assert {job["job_id"] for job in first["jobs"]}.isdisjoint(
        {job["job_id"] for job in second["jobs"]}
    )


def test_artifacts_can_be_listed_and_filtered(api: tuple[TestClient, str, Database]) -> None:
    """The other half of "show me the records": what was actually collected."""
    client, key, database = api
    from tubedepth.models import Artifact

    with database.session() as session:
        session.add(
            Artifact(
                kind="video.echo",
                target="vid00000001",
                fingerprint="fp-1",
                digest="d" * 64,
                byte_count=123,
                fetched_at=datetime(2026, 8, 19, tzinfo=UTC),
                fresh_until=datetime(2026, 9, 19, tzinfo=UTC),
            )
        )

    listed = client.get("/v1/artifacts?kind=video.echo", headers={"X-API-Key": key}).json()

    assert len(listed["artifacts"]) == 1
    assert listed["artifacts"][0]["target"] == "vid00000001"
    assert listed["artifacts"][0]["byte_count"] == 123


@pytest.mark.parametrize("route", ["/v1/jobs", "/v1/artifacts"])
@pytest.mark.parametrize("parameter", ["since", "until"])
def test_a_time_bound_without_an_offset_is_refused_rather_than_a_500(
    api: tuple[TestClient, str, Database], route: str, parameter: str
) -> None:
    """`?since=2026-08-21` — the most natural thing to type — parses to a
    *naive* datetime, and the comparison against the stored column used to go
    through `UtcDateTime.process_bind_param`, whose ValueError is neither a
    `TubedepthError` nor a `RequestValidationError`. A pure read answered 500
    `internal_error` with "refusing to store a naive datetime" — a storage
    refusal, on a request that stores nothing, outside the documented shape.
    """
    client, key, _ = api

    response = client.get(f"{route}?{parameter}=2026-08-21", headers={"X-API-Key": key})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert parameter in body["error"]["message"], "the message says which value was refused"
    assert "2026-08-21T00:00:00Z" in body["error"]["message"], (
        "the message shows the caller what an accepted timestamp looks like"
    )


@pytest.mark.parametrize("route", ["/v1/jobs", "/v1/artifacts"])
def test_a_time_bound_with_an_offset_is_accepted(
    api: tuple[TestClient, str, Database], route: str
) -> None:
    """The other half of the refusal above: aware bounds keep working, any
    offset, not just Z. `%2B` because a bare `+` in a query string is a space."""
    client, key, _ = api

    response = client.get(
        f"{route}?since=2026-08-01T00:00:00Z&until=2026-08-21T09:00:00%2B09:00",
        headers={"X-API-Key": key},
    )

    assert response.status_code == 200


@pytest.mark.parametrize("route", ["/v1/jobs", "/v1/artifacts"])
@pytest.mark.parametrize("limit", [0, -1, 100000])
def test_a_page_size_outside_the_bounds_is_refused_rather_than_clamped(
    api: tuple[TestClient, str, Database], route: str, limit: int
) -> None:
    """It was `max(1, min(limit, 500))` on both routes.

    A clamp answers 200 to a request it did not honour: `limit=100000` came
    back as 500 rows and `limit=0` as one, with nothing in the response saying
    the number had been changed. A caller paging on the size it asked for
    cannot tell that from the API agreeing with it.
    """
    client, key, _ = api

    response = client.get(f"{route}?limit={limit}", headers={"X-API-Key": key})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("route", ["/v1/jobs", "/v1/artifacts"])
def test_the_largest_page_the_reference_documents_is_accepted(
    api: tuple[TestClient, str, Database], route: str
) -> None:
    """The bound is inclusive, which is what the reference says it is."""
    client, key, _ = api

    response = client.get(f"{route}?limit={MAXIMUM_PAGE}", headers={"X-API-Key": key})

    assert response.status_code == 200


def test_the_dashboard_is_served_and_needs_no_key_to_load(
    api: tuple[TestClient, str, Database],
) -> None:
    """The page itself carries no data, so it is not what the key protects.

    It is an empty shell that asks the browser for a key and then calls the
    same `/v1` endpoints every other client uses. Putting the key requirement
    on the HTML would mean putting a secret in a URL or a cookie, and the whole
    auth design here is a header.
    """
    client, _, _ = api

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<title>" in response.text


def test_the_dashboard_ships_no_external_references(
    api: tuple[TestClient, str, Database],
) -> None:
    """A private tool on a private network cannot assume the internet.

    An external stylesheet or script would also hand a third party the pattern
    of when this instance is being looked at, which is a needless disclosure
    for an operator page.
    """
    client, _, _ = api

    page = client.get("/").text

    for marker in ("http://", "https://", "//cdn", "<script src="):
        assert marker not in page, f"the dashboard reaches outside itself: {marker}"


def test_the_dashboard_never_embeds_a_key(api: tuple[TestClient, str, Database]) -> None:
    """The page is served to anyone who can reach the port."""
    client, key, _ = api

    assert key not in client.get("/").text
    assert "ytd_" not in client.get("/").text


def test_a_submission_carries_the_bound_its_kind_deserves(
    tmp_path: Path, database: Database
) -> None:
    """The API is the third place a job is constructed, and the easiest to miss.

    A client cannot ask for a retry budget and should not: how many times a
    kind is worth trying is a property of what collecting it costs, which the
    registry knows and the submitter does not.
    """

    class ExpensivePayload(BaseModel):
        target: str

    class ExpensiveSource:
        kind = "video.expensive"
        target_type = TargetType.VIDEO
        lane = Lane.YOUTUBE
        cost = SourceCost.EXPENSIVE
        schema_version = "1"
        payload_model: type[BaseModel] = ExpensivePayload
        default_freshness = timedelta(hours=6)

        def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> ExpensivePayload:
            return ExpensivePayload(target=target)

    registry = SourceRegistry()
    registry.register(EchoSource())  # type: ignore[arg-type]
    registry.register(ExpensiveSource())  # type: ignore[arg-type]
    client = TestClient(
        create_application(
            database=database, payloads=PayloadStore(tmp_path / "payloads"), registry=registry
        )
    )
    key = ApiKeyService(database).mint(label="test").secret

    for kind in ("video.expensive", "video.echo"):
        client.post(
            "/v1/jobs",
            json={"kind": kind, "target": "dQw4w9WgXcQ"},
            headers={"X-API-Key": key},
        )

    with database.session() as session:
        bounds = {job.kind: job.max_attempts for job in session.query(Job).all()}
    assert bounds["video.expensive"] < bounds["video.echo"], (
        f"the expensive kind was submitted with as many tries as the standard one: {bounds}"
    )


def _stored_artifact(tmp_path: Path, database: Database, *, kind: str, version: str | None) -> str:
    """One artifact row and its payload, as a collection would have left them."""
    from tubedepth.models import Artifact, utcnow

    payloads = PayloadStore(tmp_path / "payloads")
    stored = payloads.put(kind, b'{"target": "dQw4w9WgXcQ", "kept": true}')
    with database.session() as session:
        session.add(
            Artifact(
                kind=kind,
                target="dQw4w9WgXcQ",
                fingerprint=f"fp-{kind}-{version}",
                schema_version=version,
                digest=stored.digest,
                byte_count=stored.byte_count,
                fetched_at=utcnow(),
                fresh_until=utcnow(),
            )
        )
    return stored.digest


def test_an_artifact_can_be_read_by_its_digest(
    api: tuple[TestClient, str, Database], tmp_path: Path
) -> None:
    """`GET /v1/artifacts` hands out digests and nothing could dereference them.

    A list route whose identifiers lead nowhere is a defect on its own terms —
    the dashboard renders the digest as a dead cell — and it is what history
    has to go through, since the alternative is keeping a job id forever.
    """
    client, key, database = api
    digest = _stored_artifact(tmp_path, database, kind="video.echo", version="1")

    response = client.get(f"/v1/artifacts/{digest}", headers={"X-API-Key": key})

    assert response.status_code == 200
    body = response.json()
    assert body["digest"] == digest
    assert body["schema_version"] == "1"
    assert body["payload"] == {"target": "dQw4w9WgXcQ", "kept": True}


def test_a_digest_this_instance_never_stored_is_not_found(
    api: tuple[TestClient, str, Database],
) -> None:
    client, key, _ = api

    response = client.get(f"/v1/artifacts/{'0' * 64}", headers={"X-API-Key": key})

    assert response.status_code == 404


def test_an_observation_from_a_retracted_version_is_gone_rather_than_served(
    api: tuple[TestClient, str, Database], tmp_path: Path, database_url_for_tests: str
) -> None:
    """`channel.about` v1 read the home tab as the about panel and returned a
    video's description as the channel's. That data is wrong rather than old,
    so the honest answer is that it was withdrawn — 410, not 404, because the
    observation happened and 404 would claim it never did.
    """
    from tubedepth.egress.control import Lane as _Lane

    class Retracted:
        kind = "channel.retracted"
        target_type = TargetType.CHANNEL
        lane = _Lane.YOUTUBE
        cost = SourceCost.CHEAP
        schema_version = "2"
        retracted_versions = frozenset({"1"})
        payload_model: type[BaseModel] = EchoPayload
        default_freshness = timedelta(hours=6)

        def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> EchoPayload:
            return EchoPayload(target=target)

    database = Database(database_url_for_tests)
    registry = SourceRegistry()
    registry.register(Retracted())  # type: ignore[arg-type]
    client = TestClient(
        create_application(
            database=database, payloads=PayloadStore(tmp_path / "payloads"), registry=registry
        )
    )
    key = ApiKeyService(database).mint(label="test").secret
    digest = _stored_artifact(tmp_path, database, kind="channel.retracted", version="1")

    response = client.get(f"/v1/artifacts/{digest}", headers={"X-API-Key": key})

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "retracted"


def test_a_batch_queues_every_target_in_one_request(
    api: tuple[TestClient, str, Database],
) -> None:
    """One charge against the allowance, not one per target.

    A key is allowed 60 requests a minute, so a hundred-video sweep submitted
    one at a time is rate-limited before it is half done — which makes the
    difference between "the API can express this" and "the API can do this".
    """
    client, key, database = api

    response = client.post(
        "/v1/jobs/batch",
        json={"kind": "video.echo", "targets": ["dQw4w9WgXcQ", "nfgdJyL-Jmg"], "refresh": True},
        headers={"X-API-Key": key},
    )

    assert response.status_code == 202
    assert len(response.json()["queued"]) == 2
    with database.session() as session:
        assert session.query(Job).count() == 2
        assert all(job.refresh for job in session.query(Job).all())


def test_a_batch_says_which_targets_it_did_not_need_to_queue(
    api: tuple[TestClient, str, Database], tmp_path: Path
) -> None:
    """Reporting rather than returning: a hundred cached payloads is megabytes,
    and the caller asked to collect, not to download."""
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
        "/v1/jobs/batch",
        json={"kind": "video.echo", "targets": ["dQw4w9WgXcQ", "nfgdJyL-Jmg"]},
        headers={"X-API-Key": key},
    )

    body = response.json()
    assert [held["target"] for held in body["held"]] == ["dQw4w9WgXcQ"]
    assert [job["target"] for job in body["queued"]] == ["nfgdJyL-Jmg"]


def test_a_batch_that_names_one_bad_target_queues_nothing(
    api: tuple[TestClient, str, Database],
) -> None:
    """All or nothing, because a partial answer to a sweep is the worst one.

    Queueing 99 of 100 and returning 202 leaves the caller believing the sweep
    ran; the missing one surfaces as an absence nobody looks for.
    """
    client, key, database = api

    response = client.post(
        "/v1/jobs/batch",
        json={"kind": "video.echo", "targets": ["dQw4w9WgXcQ", "not a video id"]},
        headers={"X-API-Key": key},
    )

    assert response.status_code == 422
    with database.session() as session:
        assert session.query(Job).count() == 0


def test_a_batch_larger_than_the_cap_is_refused(
    api: tuple[TestClient, str, Database],
) -> None:
    """An unbounded list is a way to queue a hundred thousand jobs in one
    request that the allowance was supposed to bound."""
    client, key, _ = api

    response = client.post(
        "/v1/jobs/batch",
        json={"kind": "video.echo", "targets": [f"video{index:07d}" for index in range(501)]},
        headers={"X-API-Key": key},
    )

    assert response.status_code == 422


def test_the_worker_can_be_paused_and_resumed_through_the_api(
    api: tuple[TestClient, str, Database],
) -> None:
    """The API and the worker are separate processes, so this is the channel.

    Nothing in the API can reach into the worker to stop it — that split is
    what keeps a yt-dlp crash from taking the API down — so the control is a
    row the worker reads, and this route is what writes it.
    """
    client, key, _ = api

    paused = client.patch(
        "/v1/control",
        json={"paused": True, "reason": "watching a quota"},
        headers={"X-API-Key": key},
    )
    reported = client.get("/v1/control", headers={"X-API-Key": key})
    resumed = client.patch("/v1/control", json={"paused": False}, headers={"X-API-Key": key})

    assert paused.json()["paused"] is True
    assert reported.json() == {**paused.json()}
    assert reported.json()["reason"] == "watching a quota"
    assert resumed.json()["paused"] is False


def test_control_reports_a_running_worker_before_anyone_has_touched_it(
    api: tuple[TestClient, str, Database],
) -> None:
    """No row yet is not an error; it means nobody has ever paused this."""
    client, key, _ = api

    response = client.get("/v1/control", headers={"X-API-Key": key})

    assert response.status_code == 200
    assert response.json()["paused"] is False


def test_a_batch_whose_first_target_is_uncached_does_not_deadlock(
    api: tuple[TestClient, str, Database], tmp_path: Path
) -> None:
    """The batch route's own use case, which it could not serve.

    `submit_batch` holds a write session, so from the second target on it holds
    SQLite's RESERVED lock — and `CollectionService._cached` opened the *write*
    engine to answer a question that only reads. Second `BEGIN IMMEDIATE`,
    against a lock the same request is holding: five seconds of `busy_timeout`
    and then `database is locked`.

    `decisions/002-only-writers-take-the-write-lock.md` records this exact
    shape happening once before, inside `_repair_existing_tables`.

    The order matters and is why the first two batch tests missed it: one
    passes `refresh: true`, which skips the cache check entirely, and the other
    puts the cached target first so the lock is not held yet when the second
    check runs.
    """
    from tubedepth.worker import Worker

    client, key, database = api
    registry = SourceRegistry()
    registry.register(EchoSource())  # type: ignore[arg-type]
    # Warm exactly one target, and send it *second*.
    client.post(
        "/v1/jobs",
        json={"kind": "video.echo", "target": "nfgdJyL-Jmg"},
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
        "/v1/jobs/batch",
        json={"kind": "video.echo", "targets": ["dQw4w9WgXcQ", "nfgdJyL-Jmg"]},
        headers={"X-API-Key": key},
    )

    assert response.status_code == 202
    body = response.json()
    assert [job["target"] for job in body["queued"]] == ["dQw4w9WgXcQ"]
    assert [held["target"] for held in body["held"]] == ["nfgdJyL-Jmg"]


def test_an_observation_whose_version_is_unrecorded_is_not_claimed_to_be_fine(
    tmp_path: Path,
    database_url_for_tests: str,
) -> None:
    """The window between deploying the column and running the backfill.

    `channel.about` was already at "2" before `Artifact.schema_version`
    existed, so on any real database its v1 rows hold NULL — and
    `None in frozenset({"1"})` is False. The retraction check therefore did not
    fire, and the route added to refuse a video's description presented as the
    channel's served exactly that, with a 200 and no log.

    A null version is not "fine", it is "not known". Saying so is the whole
    difference, and the message names the command that resolves it.
    """
    from tubedepth.egress.control import Lane as _Lane

    class Retracted:
        kind = "channel.retracted"
        target_type = TargetType.CHANNEL
        lane = _Lane.YOUTUBE
        cost = SourceCost.CHEAP
        schema_version = "2"
        retracted_versions = frozenset({"1"})
        payload_model: type[BaseModel] = EchoPayload
        default_freshness = timedelta(hours=6)

        def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> EchoPayload:
            return EchoPayload(target=target)

    database = Database(database_url_for_tests)
    database.create_schema()
    registry = SourceRegistry()
    registry.register(Retracted())  # type: ignore[arg-type]
    client = TestClient(
        create_application(
            database=database, payloads=PayloadStore(tmp_path / "payloads"), registry=registry
        )
    )
    key = ApiKeyService(database).mint(label="test").secret
    digest = _stored_artifact(tmp_path, database, kind="channel.retracted", version=None)

    response = client.get(f"/v1/artifacts/{digest}", headers={"X-API-Key": key})

    assert response.status_code == 409, (
        "an unattributed observation was served as though known good"
    )
    assert "backfill-schema-versions" in response.json()["error"]["message"]


class RetractedSource:
    """`channel.about`'s story in miniature: v1 withdrawn, v2 current."""

    kind = "channel.retracted"
    target_type = TargetType.CHANNEL
    lane = Lane.YOUTUBE
    cost = SourceCost.CHEAP
    schema_version = "2"
    retracted_versions = frozenset({"1"})
    payload_model: type[BaseModel] = EchoPayload
    default_freshness = timedelta(hours=6)

    def collect(self, target: str, egress: Egress, runtime: YtdlpRuntime) -> EchoPayload:
        return EchoPayload(target=target)


def _succeeded_job(database: Database, *, kind: str, digest: str) -> str:
    """One finished job row, pointing at stored bytes, as the worker leaves it."""
    with database.session() as session:
        job = Job(kind=kind, target="dQw4w9WgXcQ", state=JobState.SUCCEEDED, payload_digest=digest)
        session.add(job)
        session.flush()
        return job.identifier


def _api_with_retracted_source(tmp_path: Path, database: Database) -> tuple[TestClient, str]:
    registry = SourceRegistry()
    registry.register(RetractedSource())  # type: ignore[arg-type]
    client = TestClient(
        create_application(
            database=database, payloads=PayloadStore(tmp_path / "payloads"), registry=registry
        )
    )
    return client, ApiKeyService(database).mint(label="test").secret


def test_a_retracted_observation_is_gone_through_the_job_route_too(
    tmp_path: Path, database: Database
) -> None:
    """The same bytes, two doors (#34).

    The retraction gate was built into `GET /v1/artifacts/{digest}` and only
    there, so a client that kept its job_id and polled `/result` was served
    the identical withdrawn payload with a 200 — the mechanism protected only
    readers who happened to arrive holding a digest.
    """
    client, key = _api_with_retracted_source(tmp_path, database)
    digest = _stored_artifact(tmp_path, database, kind="channel.retracted", version="1")
    job_id = _succeeded_job(database, kind="channel.retracted", digest=digest)

    through_artifact = client.get(f"/v1/artifacts/{digest}", headers={"X-API-Key": key})
    through_job = client.get(f"/v1/jobs/{job_id}/result", headers={"X-API-Key": key})

    assert through_artifact.status_code == 410
    assert through_job.status_code == 410, (
        "the artifact route refuses these bytes as retracted; the job route served them"
    )
    body = through_job.json()
    assert body["error"]["code"] == "retracted"
    assert job_id in body["error"]["message"], (
        "the refusal should speak in terms of the id this caller actually holds"
    )


def test_an_unattributed_result_is_refused_on_both_routes(
    tmp_path: Path, database: Database
) -> None:
    """The NULL backstop, through the job door (#34).

    409 rather than 410 for the same reason as the artifact route: nothing is
    claimed to *be* retracted, only that the question cannot be answered until
    the backfill records which version collected these bytes.
    """
    client, key = _api_with_retracted_source(tmp_path, database)
    digest = _stored_artifact(tmp_path, database, kind="channel.retracted", version=None)
    job_id = _succeeded_job(database, kind="channel.retracted", digest=digest)

    through_artifact = client.get(f"/v1/artifacts/{digest}", headers={"X-API-Key": key})
    through_job = client.get(f"/v1/jobs/{job_id}/result", headers={"X-API-Key": key})

    assert through_artifact.status_code == 409
    assert through_job.status_code == 409, (
        "an unattributed payload was served through the job route as though known good"
    )
    body = through_job.json()
    assert body["error"]["code"] == "conflict"
    assert "backfill-schema-versions" in body["error"]["message"]


def test_a_shared_digest_says_how_many_observations_it_covers(
    api: tuple[TestClient, str, Database], tmp_path: Path
) -> None:
    """Identical bytes are one file, and that is half this store.

    Content addressing means a video whose counts have not moved records a new
    row against the same digest — which `GET /v1/artifacts` teaches readers to
    expect and which the hourly watch pass produces by design. On the working
    store 756 of 1,556 rows share a digest, one of them across nine
    observations spanning eight hours.

    Answering with only the newest `fetched_at` throws that away and quietly
    misdates every older duplicate. Reporting the span says the thing a series
    actually wants: nothing changed between these two times.
    """
    from tubedepth.models import Artifact, utcnow

    client, key, database = api
    payloads = PayloadStore(tmp_path / "payloads")
    stored = payloads.put("video.echo", b'{"target": "dQw4w9WgXcQ", "unchanged": true}')
    first = utcnow() - timedelta(hours=3)
    with database.session() as session:
        for offset in (3, 2, 1):
            session.add(
                Artifact(
                    kind="video.echo",
                    target="dQw4w9WgXcQ",
                    fingerprint="fp",
                    schema_version="1",
                    digest=stored.digest,
                    byte_count=stored.byte_count,
                    fetched_at=utcnow() - timedelta(hours=offset),
                    fresh_until=utcnow(),
                )
            )

    body = client.get(f"/v1/artifacts/{stored.digest}", headers={"X-API-Key": key}).json()

    assert body["observations"] == 3
    assert body["first_fetched_at"] is not None
    assert body["first_fetched_at"][:13] == first.isoformat()[:13]
    assert body["fetched_at"] > body["first_fetched_at"]


def test_a_missing_payload_does_not_blame_retention_for_it(
    api: tuple[TestClient, str, Database], tmp_path: Path
) -> None:
    """The index row is two days old and the bytes are gone. Retention is 30 days.

    "It has aged out of retention" is the one explanation this route cannot
    check and the one it used to give unconditionally. The other explanation is
    that the index and the payload store were separated — a cutover that moved
    the database and not `TUBEDEPTH_DATA_DIR` — and telling an operator their
    fresh observation expired sends them to look for a retention bug that is
    not there.
    """
    client, key, database = api
    digest = _stored_artifact(tmp_path, database, kind="video.echo", version="1")
    PayloadStore(tmp_path / "payloads").delete("video.echo", digest)

    response = client.get(f"/v1/artifacts/{digest}", headers={"X-API-Key": key})

    assert response.status_code == 404
    message = response.json()["error"]["message"]
    assert "it has aged out of retention" not in message, (
        "a two-day-old observation was told, as a fact, that it expired"
    )
    assert "TUBEDEPTH_DATA_DIR" in message, "the other explanation is not offered"
