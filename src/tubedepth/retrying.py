"""When to try again, and how long to wait.

Retryability is a property of the failure — see errors.TubedepthError — and
this module is the arithmetic around it.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import timedelta

from .errors import TubedepthError

BASE_DELAY_SECONDS = 15.0
# Unbounded doubling reaches days, which is indistinguishable from the job
# never running again, and hides the failure rather than surfacing it.
MAXIMUM_DELAY_SECONDS = 30 * 60.0
JITTER_RANGE = (0.5, 1.5)


def is_retryable(error: BaseException) -> bool:
    return isinstance(error, TubedepthError) and error.retryable


def _default_jitter() -> float:
    return random.uniform(*JITTER_RANGE)


def backoff_for_attempt(
    attempt: int, *, jitter: Callable[[], float] = _default_jitter
) -> timedelta:
    """How long to wait before attempt number `attempt + 1`.

    Jittered because a bot check does not hit one job — it hits every job in
    flight. Retrying them all at the same instant reproduces the burst that
    caused it, which is how a soft block becomes a hard one.
    """
    exponential = BASE_DELAY_SECONDS * 2 ** max(0, attempt - 1)
    return timedelta(seconds=min(MAXIMUM_DELAY_SECONDS, exponential) * jitter())
