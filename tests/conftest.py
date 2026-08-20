"""Shared fixtures, and the guard that keeps the suite honest.

The socket guard below is the reason CI can be trusted. Every data source in
this project talks to YouTube or a third-party service, so a test that quietly
reaches the network passes on a residential laptop and fails on a GitHub
runner — whose datacenter address YouTube bot-checks — and the failure looks
like a bug in the code rather than in the test. Blocking connect() turns that
from an intermittent red build into an immediate, named failure at the moment
the offending test is written.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tubedepth.database import Database

FIXTURE_ROOT = Path(__file__).parent / "fixtures"

# The addresses a database connection is allowed to reach even though this is
# the offline suite. `just postgres` (and CI's service container) put the
# server on localhost, never on the network `refuse_outbound_network` exists
# to guard — a real hostname would still be refused.
LOCAL_DATABASE_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def pytest_addoption(parser: pytest.Parser) -> None:
    """`--record-payload-shapes` updates the payload shape lock.

    An option rather than a command, because the thing being recorded is what
    the tests compare against and it has no runtime caller — a canonicalizer in
    `src/` would be a capability nothing in production calls, which is the
    failure `decisions/003` is about.
    """
    parser.addoption(
        "--record-payload-shapes",
        action="store_true",
        default=False,
        help="Append the current payload shapes to tests/payload_shapes.json",
    )


@pytest.fixture(autouse=True)
def refuse_outbound_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Fail any test not marked `live` that tries to reach a real network address.

    httpx's ASGITransport and MockTransport never reach this, and neither does
    anything reading a fixture, so nothing legitimate is affected.

    A connection to `LOCAL_DATABASE_HOSTS` is let through rather than refused
    outright. Now that `database_url_for_tests` names a real PostgreSQL server
    (`just postgres`, or CI's service container) instead of a SQLite file, the
    whole default suite opens a socket to it — that traffic is neither the
    network nor the hazard this guard exists for. Anything else, including a
    datacenter-only address a proxy might resolve to, is still refused.
    """
    if request.node.get_closest_marker("live"):
        yield
        return

    original_connect = socket.socket.connect

    def refuse(self: socket.socket, address: object) -> object:
        host = address[0] if isinstance(address, tuple) else address
        if host in LOCAL_DATABASE_HOSTS:
            return original_connect(self, address)  # type: ignore[arg-type]
        raise RuntimeError(
            f"test attempted a network connection to {address!r}; "
            "use a fixture, respx, or mark the test with @pytest.mark.live"
        )

    monkeypatch.setattr(socket.socket, "connect", refuse)
    yield


@pytest.fixture(autouse=True)
def refuse_an_ambient_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator's shell is not a fixture, and must not become one.

    `_database()` honours `TUBEDEPTH_DATABASE_URL` now (that is the point of
    this cutover), which means a value already sitting in the environment —
    naming the shared fleet PostgreSQL, say — used to be inert here and is not
    any more: it would redirect every test that goes through `_database()` or
    constructs a bare `Database(...)`, `just check` included. Deleting it for
    every test is what keeps `database_url_for_tests` the only seam that names
    a database in this suite, the same discipline `refuse_outbound_network`
    applies to sockets.
    """
    monkeypatch.delenv("TUBEDEPTH_DATABASE_URL", raising=False)


@pytest.fixture
def fixture_root() -> Path:
    return FIXTURE_ROOT


def _schema_name_for(nodeid: str) -> str:
    """A PostgreSQL identifier derived from a test's node id.

    Readable where it can be — a failure in `psql \\dn` output should still
    say roughly which test left a schema behind — but PostgreSQL identifiers
    top out at 63 bytes and a parametrized node id routinely blows past that,
    so the digest is what actually guarantees two different tests never
    collide on a truncated prefix.
    """
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", nodeid).strip("_").lower()
    digest = hashlib.sha1(nodeid.encode()).hexdigest()[:10]
    return f"t_{slug[: 63 - len(digest) - 2]}_{digest}"


@pytest.fixture
def database_url_for_tests(request: pytest.FixtureRequest) -> Iterator[str]:
    """The one place the suite names a database.

    Every test module used to write `Database(tmp_path / "tubedepth.db")`,
    fifty-nine times across eighteen files, so "the tests move to PostgreSQL"
    meant fifty-nine edits. It is one now.

    A schema per test, not a database per test: creating a PostgreSQL database
    is seconds each, and this suite has hundreds of tests. The schema is named
    for the test itself, created on the server named by
    `TUBEDEPTH_TEST_POSTGRES_URL` (the migrator — the same credential the
    `postgres`-marked structural tests use, so it already has `CREATE ON
    DATABASE` from the test harness) and dropped again on teardown regardless
    of outcome, so a failed test does not leave the next run something to trip
    over.

    Connected to directly, with no `SET ROLE`: the schema is created with no
    explicit `AUTHORIZATION`, so it is owned by the connecting role outright,
    and the migrator can create and touch tables in it without borrowing
    `tubedepth_owner`'s privileges the way `migrations/env.py` has to. The
    three-role separation this buys production is proven against the literal
    `tubedepth` schema by `tests/test_postgres_privileges.py`, not by every
    other test in the suite reconstructing it.

    The yielded URL points the connection's `search_path` at the new schema
    via `options`, not by naming it in `Database.SCHEMA` (which stays
    `"tubedepth"` — that constant is what `verify_placement()` and
    `is_migrated()` check against production's fixed schema name, and no
    ordinary test through this fixture calls either).
    """
    server_url = os.environ.get("TUBEDEPTH_TEST_POSTGRES_URL")
    if not server_url:
        pytest.skip("set TUBEDEPTH_TEST_POSTGRES_URL, or run `just postgres`")

    schema = _schema_name_for(request.node.nodeid)
    admin_engine = create_engine(server_url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin_engine.dispose()

    url = make_url(server_url).update_query_dict({"options": f"-csearch_path={schema},pg_catalog"})

    try:
        # Not `str(url)`: SQLAlchemy's `URL.__str__` masks the password as
        # `***` by default (it is written where it might end up in a log), so
        # every connection through the yielded URL would fail authentication
        # rather than reach the schema this fixture just created.
        yield url.render_as_string(hide_password=False)
    finally:
        admin_engine = create_engine(server_url)
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.fixture
def database(database_url_for_tests: str) -> Database:
    database = Database(database_url_for_tests)
    database.create_schema()
    return database
