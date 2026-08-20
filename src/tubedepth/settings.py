"""Where the runtime reads its environment, in one place.

`TUBEDEPTH_DATABASE_URL` used to be read by `migrations/env.py` and by nothing
else, so `tubedepth migrate` and `tubedepth work` could name different
databases and neither would say so. One resolver, called by both — and, since
the cutover (#15), the only place a database is ever named: there is no
SQLite fallback to fall back to.
"""

from __future__ import annotations

import os

from .errors import ConfigurationError


def database_url() -> str:
    """The PostgreSQL URL to connect through.

    Before the cutover this fell back to a SQLite file under the data
    directory when the variable was unset — that let a fresh checkout run
    with nothing configured, at the price of two dialects to keep correct
    forever. There is no fallback left: an unset variable is a configuration
    error to report, not a file to open quietly in its place.
    """
    explicit = os.environ.get("TUBEDEPTH_DATABASE_URL")
    if not explicit:
        raise ConfigurationError(
            "TUBEDEPTH_DATABASE_URL is not set. tubedepth runs on PostgreSQL only "
            "since the cutover (#15) and has no SQLite fallback — point it at the "
            "shared instance, e.g. postgresql+psycopg://tubedepth_runtime:...@host/db"
        )
    return explicit
