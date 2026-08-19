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
    if request.node.get_closest_marker("live") is not None:
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
