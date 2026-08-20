"""Adaptive rate control, per (egress, lane).

The rule is TCP congestion avoidance, for the same reason TCP uses it: the safe
sending rate is not knowable in advance, it changes, and the cost of guessing
high is losing the route. Additive increase finds the ceiling slowly;
multiplicative decrease leaves it fast.

The key is (egress, lane) rather than (egress, backend) because the thing that
rate-limits us is a service, not our internal taxonomy. yt-dlp, InnerTube and a
caption fetch all draw on the same per-address Google tolerance; SponsorBlock
has its own budget and its own 429.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum

from ..errors import RateLimitedError, UpstreamError


class Lane(StrEnum):
    """A rate-limit domain — a service, not a backend."""

    YOUTUBE = "youtube"
    SPONSORBLOCK = "sponsorblock"
    # Google's API quota is a different budget from YouTube's tolerance for an
    # address: 10,000 units a day, spent a unit at a time, and untouched by
    # anything the other lanes do. Sharing a lane would make a quarantine on
    # one throttle the other for no reason in either direction.
    YOUTUBE_DATA_API = "youtube_data_api"


class Verdict(StrEnum):
    """What an attempt tells us about the route it used."""

    OK = "ok"
    THROTTLED = "throttled"
    BLOCKED = "blocked"  # this address is burned; the request itself was fine
    NEUTRAL = "neutral"  # the attempt says nothing about the route either way


def verdict_for_error(error: BaseException) -> Verdict:
    """What a failed job tells the controller about the line it went out on.

    Most failures tell it nothing. A video with captions turned off, a bad
    identifier, a renderer that no longer matches — the request went out and
    came back exactly as the network intended, and the disappointment is about
    the content. Reporting those as throttling is how an address that is
    working perfectly gets slowed to a standstill by the videos it fetched.

    Measured: seven caption-less videos in a forty-job sweep doubled the lane's
    minimum interval on each one and took the run from roughly ninety jobs a
    minute to four.

    So the default is NEUTRAL, and only the two failures that are genuinely
    about the route move anything.
    """
    if isinstance(error, RateLimitedError):
        return Verdict.BLOCKED
    if isinstance(error, UpstreamError):
        return Verdict.THROTTLED
    return Verdict.NEUTRAL


QUARANTINE_BASE_SECONDS = 300.0
QUARANTINE_CEILING_SECONDS = 3_600.0

# How many consecutive successes clear the streak. High enough that one lucky
# request does not reset a genuine block, low enough that a recovered address
# stops paying for last week.
RECOVERY_SUCCESSES = 20

# Concurrency and rate are different limits, and both matter. A window of four
# with no spacing is four requests in the same millisecond, which is the shape
# that gets an address flagged even when the count is modest.
DEFAULT_MINIMUM_INTERVAL_SECONDS = 0.0
MAXIMUM_INTERVAL_SECONDS = 60.0


@dataclass(slots=True)
class LaneState:
    window: float = 1.0
    quarantined_until: float = 0.0
    quarantine_streak: int = 0
    recovery_successes: int = 0
    in_flight: int = 0
    minimum_interval: float = DEFAULT_MINIMUM_INTERVAL_SECONDS
    next_earliest_start: float = 0.0


@dataclass(slots=True)
class RateController:
    # time.monotonic, never the wall clock: this runs on WSL2, where the wall
    # clock jumps after the Windows host sleeps, and a jump would release every
    # quarantine at once.
    clock: Callable[[], float] = time.monotonic
    minimum_interval_seconds: float = DEFAULT_MINIMUM_INTERVAL_SECONDS
    # The ceiling additive increase may never pass. Without it a route that
    # keeps succeeding grows without bound and finds the real limit the hard way.
    window_ceiling: float = 6.0
    _states: dict[tuple[str, Lane], LaneState] = field(default_factory=dict)
    # Shared across worker threads. Without the lock, two threads both read an
    # in-flight count of zero against a window of one and both proceed — the
    # exact over-sending this class exists to prevent, visible only under load.
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _state(self, egress: str, lane: Lane) -> LaneState:
        state = self._states.get((egress, lane))
        if state is None:
            state = LaneState(minimum_interval=self.minimum_interval_seconds)
            self._states[(egress, lane)] = state
        return state

    def observed(self, egress: str, lane: Lane) -> tuple[LaneState, float]:
        """A copy of one lane's state, and the clock reading it goes with.

        Both together, because the deadlines in that state are `time.monotonic`
        readings and a difference against a *later* reading would understate
        the quarantine. Anyone converting them to a wall clock needs the same
        instant the state was taken at.
        """
        with self._lock:
            return replace(self._state(egress, lane)), self.clock()

    def window(self, egress: str, lane: Lane) -> float:
        return self._state(egress, lane).window

    def is_available(self, egress: str, lane: Lane) -> bool:
        with self._lock:
            return self.clock() >= self._state(egress, lane).quarantined_until

    def acquire(self, egress: str, lane: Lane) -> bool:
        """Take a slot if the window, the quarantine and the spacing all allow it.

        Refusing is the normal case rather than an error: the caller waits and
        asks again, or asks a different egress.
        """
        with self._lock:
            now = self.clock()
            state = self._state(egress, lane)
            if now < state.quarantined_until:
                return False
            if state.in_flight >= max(1, int(state.window)):
                return False
            if now < state.next_earliest_start:
                return False
            state.in_flight += 1
            # Claimed before the request starts, so concurrent callers cannot
            # all clear the spacing gate on the same tick.
            state.next_earliest_start = now + state.minimum_interval
            return True

    def release(self, egress: str, lane: Lane, verdict: Verdict) -> None:
        with self._lock:
            state = self._state(egress, lane)
            state.in_flight = max(0, state.in_flight - 1)
            self._apply(state, verdict, self.clock())

    @contextmanager
    def permit(self, egress: str, lane: Lane) -> Iterator[None]:
        """Hold a slot for the duration of a request.

        The verdict defaults to a failure that does not widen the window: an
        exception escaping the body is not evidence the route can take more.
        """
        verdict = Verdict.THROTTLED
        try:
            yield
            verdict = Verdict.OK
        finally:
            self.release(egress, lane, verdict)

    def record(self, egress: str, lane: Lane, verdict: Verdict) -> None:
        with self._lock:
            self._apply(self._state(egress, lane), verdict, self.clock())

    def _apply(self, state: LaneState, verdict: Verdict, now: float) -> None:
        if verdict is Verdict.NEUTRAL:
            return
        if verdict is Verdict.OK:
            state.window = min(self.window_ceiling, state.window + 1.0 / state.window)
            state.minimum_interval = max(
                self.minimum_interval_seconds, state.minimum_interval * 0.95
            )
            state.recovery_successes += 1
            if state.recovery_successes >= RECOVERY_SUCCESSES:
                state.quarantine_streak = 0
        elif verdict is Verdict.THROTTLED:
            # Floored at one: a window below 1 admits nothing, and an egress
            # that admits nothing can never earn the success that would let
            # it recover.
            state.window = max(1.0, state.window / 2.0)
            state.minimum_interval = min(
                MAXIMUM_INTERVAL_SECONDS, max(1.0, state.minimum_interval * 2.0)
            )
            # Push the gate out too. Widening the interval without moving the
            # next allowed start applies the new spacing one request late —
            # which is one request too many, immediately after being told to
            # slow down.
            state.next_earliest_start = now + state.minimum_interval
        elif verdict is Verdict.BLOCKED:
            # Back into slow start, not back to the window it had: the address
            # was just told it was going too fast.
            state.window = 1.0
            state.quarantine_streak += 1
            state.recovery_successes = 0
            backoff = min(
                QUARANTINE_CEILING_SECONDS,
                QUARANTINE_BASE_SECONDS * 2 ** (state.quarantine_streak - 1),
            )
            state.quarantined_until = self.clock() + backoff
