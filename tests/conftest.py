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

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from tubedepth.database import Database

FIXTURE_ROOT = Path(__file__).parent / "fixtures"


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
    """Fail any test not marked `live` that tries to open a socket.

    httpx's ASGITransport and MockTransport never reach this, and neither does
    anything reading a fixture, so nothing legitimate is affected.
    """
    # `postgres` too: those tests talk to a database server on localhost, which
    # is neither the network nor the hazard this guard exists for. They are
    # deselected by default for the same reason `live` is — they need something
    # the offline suite must not assume is there.
    if any(request.node.get_closest_marker(name) for name in ("live", "postgres")):
        yield
        return

    def refuse(self: socket.socket, address: object) -> None:
        raise RuntimeError(
            f"test attempted a network connection to {address!r}; "
            "use a fixture, respx, or mark the test with @pytest.mark.live"
        )

    monkeypatch.setattr(socket.socket, "connect", refuse)
    yield


@pytest.fixture
def fixture_root() -> Path:
    return FIXTURE_ROOT


@pytest.fixture
def database_url_for_tests(tmp_path: Path) -> str:
    """The one place the suite names a database.

    Every test module used to write `Database(tmp_path / "tubedepth.db")`,
    fifty-nine times across eighteen files, so "the tests move to PostgreSQL"
    meant fifty-nine edits. It is one now.
    """
    return f"sqlite+pysqlite:///{tmp_path / 'tubedepth.db'}"


@pytest.fixture
def database(database_url_for_tests: str) -> Database:
    database = Database(database_url_for_tests)
    database.create_schema()
    return database
