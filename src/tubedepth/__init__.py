"""Scraped YouTube data the official Data API does not expose."""

from __future__ import annotations

# The one place this project's version is written.
#
# `pyproject.toml` declares its version dynamic and reads this line, so a
# release bumps one number. Everything that reports a version — `tubedepth
# version`, `/healthz`, the OpenAPI document — imports it from here, and
# `docs/releasing.md` says what else a bump has to be accompanied by.
__version__ = "0.1.0"

__all__ = ["__version__"]
