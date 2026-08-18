"""Domain errors, caught once at each boundary.

One base class with a small taxonomy under it, so the CLI, the HTTP API and the
worker loop each need exactly one catch site rather than a growing list of
except clauses. Messages are lowercase, carry no trailing period, and name the
offending value — they reach API clients verbatim.
"""

from __future__ import annotations

from typing import ClassVar


class TubedepthError(Exception):
    """Base for domain errors that are safe to show a CLI user or an API client.

    `retryable` lives on the class rather than at the call site because the
    answer is a property of the failure, not of who caught it — and because
    deciding it per call site is how the same failure ends up retried in one
    place and not in another.
    """

    retryable: ClassVar[bool] = False


class ValidationError(TubedepthError):
    """The request could not be understood: a malformed identifier, a bad option."""


class NotFoundError(TubedepthError):
    """The thing asked for does not exist here, or the video does not have it."""


class UpstreamError(TubedepthError):
    """A backend answered, and the answer was unusable.

    Retried: a reset connection, a 5xx and a truncated body all look like this
    and all of them are worth one more go.
    """

    retryable: ClassVar[bool] = True


class ConfigurationError(TubedepthError):
    """Our own misconfiguration. Never the client's fault."""


class RateLimitedError(UpstreamError):
    """The upstream told us to slow down, or refused this address outright.

    Retried, but the rate controller has already narrowed the window and
    lengthened the interval by the time this is handled, so the retry goes out
    slower than the request that caused it.
    """

    retryable: ClassVar[bool] = True


class ExtractionError(TubedepthError):
    """A backend answered and the response no longer contains what the parser needs.

    Kept distinct from every other upstream failure, and never retried. It is
    not a network problem and it is not transient: retrying spends requests
    against an address that answered perfectly well, and the only thing that
    fixes it is a code change. Keeping it its own type is what lets the worker
    refuse to retry it and lets an operator see it for what it is.
    """
