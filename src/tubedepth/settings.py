"""Where the runtime reads its environment, in one place.

`TUBEDEPTH_DATABASE_URL` was read by `migrations/env.py` and by nothing else,
so `tubedepth migrate` and `tubedepth work` could name different databases and
neither would say so. One resolver, called by both.
"""

from __future__ import annotations

import os
from pathlib import Path


def database_url(data_directory: Path) -> str:
    """The database to use, environment first.

    The fallback names a SQLite file under the data directory, which is what
    a checkout with nothing configured gets. `TUBEDEPTH_DATA_DIR` keeps
    meaning the payload store: after the cutover a deployment has a URL and a
    directory, and naming the directory must not be able to redirect the
    database.
    """
    explicit = os.environ.get("TUBEDEPTH_DATABASE_URL")
    if explicit:
        return explicit
    return f"sqlite+pysqlite:///{data_directory / 'tubedepth.db'}"
