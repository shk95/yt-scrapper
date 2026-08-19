"""Logging setup, and the one thing it must not do.

httpx logs every request line at INFO, URL included. Caption URLs carry a
`signature` query parameter, so the default configuration writes a credential
into the log on every transcript job — the same credential this project takes
care never to put in an artifact. A log file is storage too.
"""

from __future__ import annotations

import logging

import respx

from tubedepth.egress.transport import DirectEgress
from tubedepth.observability import configure_logging

SIGNED_CAPTION_URL = (
    "https://www.youtube.com/api/timedtext"
    "?v=dQw4w9WgXcQ&signature=55A590D639C96D0597127C9F17C46E1FB7DF8F2B&fmt=json3"
)


@respx.mock
def test_a_signed_url_is_never_written_to_the_log(caplog) -> None:  # type: ignore[no-untyped-def]
    respx.get(SIGNED_CAPTION_URL).respond(200, json={"events": []})
    configure_logging(level="DEBUG")

    with caplog.at_level(logging.DEBUG):
        DirectEgress().fetch(SIGNED_CAPTION_URL)

    written = "\n".join(record.getMessage() for record in caplog.records)
    assert "signature=" not in written, f"a signed URL reached the log:\n{written}"


def test_configuring_logging_still_lets_this_project_log() -> None:
    # Quieting httpx must not be done by quieting everything: the worker's own
    # progress lines are how anyone finds out what happened overnight.
    configure_logging(level="INFO")

    assert logging.getLogger("tubedepth.worker").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
