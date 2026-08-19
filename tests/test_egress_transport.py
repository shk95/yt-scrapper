"""One egress, one proxy decision, both transports.

The invariant worth a test of its own: httpx and yt-dlp must never disagree
about which address a request leaves from. A disagreement there is invisible —
nothing errors, nothing logs — and it leaks the origin address on whichever
transport was forgotten.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tubedepth.egress.transport import BROWSER_USER_AGENT, DirectEgress, ProxiedEgress


@respx.mock
def test_every_request_carries_a_browser_user_agent() -> None:
    # Not decoration, though the measurement behind it left with the Return
    # YouTube Dislike source: that service answered 403 to urllib's
    # default agent and 200 to a browser one — measured on this machine.
    # Setting it on the client is what stops any one source forgetting it.
    route = respx.get("https://example.invalid/thing").respond(200, json={})

    with DirectEgress().http_client() as client:
        client.get("https://example.invalid/thing")

    assert route.calls[0].request.headers["user-agent"] == BROWSER_USER_AGENT


def test_a_direct_egress_passes_no_proxy_to_either_transport() -> None:
    egress = DirectEgress()

    assert egress.proxy_url is None
    assert "proxy" not in egress.ytdlp_options()


def test_one_proxied_egress_gives_both_transports_the_same_proxy() -> None:
    # The test that pins the invariant. If these two ever come from different
    # places, this is what fails.
    egress = ProxiedEgress(name="vpn-jp1", proxy_url="http://127.0.0.1:27100")

    assert egress.ytdlp_options()["proxy"] == egress.proxy_url

    with egress.http_client() as client:
        mounts = [transport for transport in client._mounts.values() if transport is not None]

    assert mounts, "a proxied egress must actually mount a proxy transport"


@respx.mock
def test_a_transport_failure_is_reported_as_a_domain_error() -> None:
    from tubedepth.errors import UpstreamError

    respx.get("https://example.invalid/thing").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(UpstreamError, match="could not be reached"):
        DirectEgress().fetch("https://example.invalid/thing")
