"""Adaptive rate control, per (egress, lane).

The rule is TCP congestion avoidance, for the same reason TCP uses it: the safe
sending rate is not knowable in advance, it changes, and the cost of guessing
high is losing the route. Additive increase finds the ceiling slowly;
multiplicative decrease leaves it fast.

The key is (egress, lane) rather than (egress, backend) because the thing that
rate-limits us is a service, not our internal taxonomy. yt-dlp, InnerTube and a
caption fetch all draw on the same per-address Google tolerance; Return YouTube
Dislike and SponsorBlock each have their own budget and their own 429.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class Lane(StrEnum):
    """A rate-limit domain — a service, not a backend."""

    YOUTUBE = "youtube"
    RYD = "ryd"
    SPONSORBLOCK = "sponsorblock"


class Verdict(StrEnum):
    """What an attempt tells us about the route it used."""

    OK = "ok"
    THROTTLED = "throttled"
    BLOCKED = "blocked"  # this address is burned; the request itself was fine


QUARANTINE_BASE_SECONDS = 300.0
QUARANTINE_CEILING_SECONDS = 3_600.0

# How many consecutive successes clear the streak. High enough that one lucky
# request does not reset a genuine block, low enough that a recovered address
# stops paying for last week.
RECOVERY_SUCCESSES = 20


@dataclass(slots=True)
class LaneState:
    window: float = 1.0
    quarantined_until: float = 0.0
    quarantine_streak: int = 0
    recovery_successes: int = 0


@dataclass(slots=True)
class RateController:
    # time.monotonic, never the wall clock: this runs on WSL2, where the wall
    # clock jumps after the Windows host sleeps, and a jump would release every
    # quarantine at once.
    clock: Callable[[], float] = time.monotonic
    _states: dict[tuple[str, Lane], LaneState] = field(default_factory=dict)

    def _state(self, egress: str, lane: Lane) -> LaneState:
        return self._states.setdefault((egress, lane), LaneState())

    def window(self, egress: str, lane: Lane) -> float:
        return self._state(egress, lane).window

    def is_available(self, egress: str, lane: Lane) -> bool:
        return self.clock() >= self._state(egress, lane).quarantined_until

    def record(self, egress: str, lane: Lane, verdict: Verdict) -> None:
        state = self._state(egress, lane)
        if verdict is Verdict.OK:
            state.window += 1.0 / state.window
            state.recovery_successes += 1
            if state.recovery_successes >= RECOVERY_SUCCESSES:
                state.quarantine_streak = 0
        elif verdict is Verdict.THROTTLED:
            # Floored at one: a window below 1 admits nothing, and an egress
            # that admits nothing can never earn the success that would let
            # it recover.
            state.window = max(1.0, state.window / 2.0)
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
