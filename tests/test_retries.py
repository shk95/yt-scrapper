"""What gets another go, and how long it waits.

Retryability is a property of the failure, not of the call site. Deciding it
where the error is raised means the worker cannot get it wrong, and it means
the answer is the same whether the failure came from the CLI or the queue.
"""

from __future__ import annotations

import pytest

from tubedepth.errors import (
    ConfigurationError,
    NotFoundError,
    RateLimitedError,
    UpstreamError,
    ValidationError,
)
from tubedepth.retrying import backoff_for_attempt, is_retryable


def test_a_rate_limit_is_worth_retrying() -> None:
    assert is_retryable(RateLimitedError("slow down")) is True


def test_an_unusable_upstream_answer_is_worth_retrying() -> None:
    assert is_retryable(UpstreamError("connection reset")) is True


def test_a_malformed_request_is_never_retried() -> None:
    # Nothing about waiting makes a bad video id into a good one, and the
    # retry costs a request against the same address that just refused it.
    assert is_retryable(ValidationError("video identifier is not valid: x")) is False


def test_a_missing_thing_is_never_retried() -> None:
    assert is_retryable(NotFoundError("no caption track for language: en")) is False


def test_our_own_misconfiguration_is_never_retried() -> None:
    assert is_retryable(ConfigurationError("source kind is already registered")) is False


def test_the_backoff_grows_with_each_attempt() -> None:
    first = backoff_for_attempt(1, jitter=lambda: 1.0)
    second = backoff_for_attempt(2, jitter=lambda: 1.0)
    third = backoff_for_attempt(3, jitter=lambda: 1.0)

    assert first < second < third
    assert second.total_seconds() == pytest.approx(first.total_seconds() * 2)


def test_the_backoff_stops_growing_at_a_ceiling() -> None:
    # Unbounded doubling reaches days, which is indistinguishable from the job
    # never running again — and it hides the failure instead of surfacing it.
    huge = backoff_for_attempt(40, jitter=lambda: 1.0)

    assert huge.total_seconds() <= 30 * 60


def test_the_backoff_is_jittered() -> None:
    """Without jitter, everything that failed together retries together.

    A bot check does not hit one job; it hits every job in flight. Retrying
    them all at the same instant reproduces the burst that caused it.
    """
    low = backoff_for_attempt(3, jitter=lambda: 0.5)
    high = backoff_for_attempt(3, jitter=lambda: 1.5)

    assert low < high
