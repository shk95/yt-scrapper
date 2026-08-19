"""Logging setup for anything that runs unattended.

A deliberate departure from the house style, which uses typer.echo and the
→ ✓ ✗ · vocabulary everywhere. That breaks down for a worker running for days
under systemd: echo from a background loop has no timestamp, no level and no
ordering, and "which of last night's four hundred jobs hit the bot check" is
then unanswerable. Interactive CLI output is unchanged.
"""

from __future__ import annotations

import logging
import os

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"

# Loggers that write a request URL at INFO. Caption URLs carry a `signature`
# query parameter, so leaving httpx at INFO writes a live credential into the
# log on every transcript job — the same credential this project refuses to put
# in an artifact. A log file is storage too.
URL_LOGGING_LIBRARIES = ("httpx", "httpcore", "hpack")


def configure_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=(level or os.environ.get("TUBEDEPTH_LOG_LEVEL", "INFO")).upper(),
        format=LOG_FORMAT,
        force=True,
    )
    for name in URL_LOGGING_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
