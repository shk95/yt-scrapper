"""Domain errors, caught once at each boundary.

One base class with a small taxonomy under it, so the CLI, the HTTP API and the
worker loop each need exactly one catch site rather than a growing list of
except clauses. Messages are lowercase, carry no trailing period, and name the
offending value — they reach API clients verbatim.
"""

from __future__ import annotations


class TubedepthError(Exception):
    """Base for domain errors that are safe to show a CLI user or an API client."""


class ValidationError(TubedepthError):
    """The request could not be understood: a malformed identifier, a bad option."""
