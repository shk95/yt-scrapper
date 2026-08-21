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


TRUE_SPELLINGS = frozenset({"1", "true", "yes", "on"})
FALSE_SPELLINGS = frozenset({"0", "false", "no", "off"})


def api_key_required() -> bool:
    """Whether `/v1` demands an `X-API-Key`. Off unless asked for.

    This service is deployed on a private network, reached by the other
    services in the fleet and by nobody else, and there minting a key per
    caller bought an audit column and a rate limiter at the price of a secret
    to distribute and rotate. So the default is off: no header, no 401.

    It is a switch rather than a deletion because the difference between "on a
    private network" and "reachable" is one firewall rule, and the day that
    rule changes the fix has to be one variable rather than a revert. Keys are
    still minted, listed and revoked by `tubedepth key`, jobs still carry
    `api_key_id` when one was presented, and turning this on restores every
    401 and the per-key allowance exactly as they were.

    A value that is neither a yes nor a no is refused rather than read as a
    no: `TUBEDEPTH_REQUIRE_API_KEY=treu` silently serving an open API is the
    failure this whole variable exists to make visible.
    """
    raw = os.environ.get("TUBEDEPTH_REQUIRE_API_KEY")
    if raw is None or not raw.strip():
        return False
    spelled = raw.strip().lower()
    if spelled in TRUE_SPELLINGS:
        return True
    if spelled in FALSE_SPELLINGS:
        return False
    raise ConfigurationError(
        f"TUBEDEPTH_REQUIRE_API_KEY={raw!r} is neither a yes nor a no. "
        f"Write one of {sorted(TRUE_SPELLINGS)} or {sorted(FALSE_SPELLINGS)}"
    )
