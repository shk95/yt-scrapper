"""The adaptive rate controller.

Nobody — not us, not the user, not YouTube's documentation — can say how many
requests per second one address survives. It moves with the time of day, the
video, and whether cookies are attached. So the controller measures instead of
assuming, and these tests pin the measuring rule rather than any particular
number. An injected clock and a seeded RNG make the whole thing deterministic,
so none of this sleeps and none of it touches the network.
"""

from __future__ import annotations

import pytest

from tubedepth.egress.control import Lane, RateController, Verdict


class FakeClock:
    """A monotonic clock the test drives by hand.

    The controller reads time.monotonic() in production — never the wall clock,
    because this runs on WSL2 where the wall clock jumps after the Windows host
    sleeps, and a jumped clock would release every quarantine at once.
    """

    def __init__(self, now: float = 1_000.0) -> None:
        self._now = now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def test_a_success_at_the_starting_window_grows_it_by_one() -> None:
    # Additive increase is `window += 1 / window`, so one full window of
    # successes buys one extra concurrent slot. At the starting window of 1.0
    # that is a single success.
    controller = RateController()

    controller.record("direct", Lane.YOUTUBE, Verdict.OK)

    assert controller.window("direct", Lane.YOUTUBE) == pytest.approx(2.0)


def test_a_throttle_halves_the_window() -> None:
    # Multiplicative decrease. Leaving fast is the whole point: the cost of
    # guessing high is losing the route, so a throttle gives back half of
    # everything the additive increase earned.
    controller = RateController()
    controller.record("direct", Lane.YOUTUBE, Verdict.OK)  # 1.0 -> 2.0
    controller.record("direct", Lane.YOUTUBE, Verdict.OK)  # 2.0 -> 2.5

    controller.record("direct", Lane.YOUTUBE, Verdict.THROTTLED)

    assert controller.window("direct", Lane.YOUTUBE) == pytest.approx(1.25)


def test_the_window_never_falls_below_one_slot() -> None:
    # A window under 1 would admit nothing at all, and an egress that admits
    # nothing never earns the success that would let it recover. Halving has to
    # bottom out at one in-flight request.
    controller = RateController()

    for _ in range(5):
        controller.record("direct", Lane.YOUTUBE, Verdict.THROTTLED)

    assert controller.window("direct", Lane.YOUTUBE) == pytest.approx(1.0)


def test_throttling_one_lane_leaves_another_lane_on_the_same_egress_untouched() -> None:
    # This is the reason the key is (egress, lane) and not (egress, backend).
    # Return YouTube Dislike documents 100 requests per minute; YouTube
    # documents nothing and tolerates far more. One address hitting RYD's limit
    # says nothing whatsoever about what that address may still do against
    # YouTube, and collapsing the two would throw away most of the throughput.
    controller = RateController()
    controller.record("vpn-jp1", Lane.YOUTUBE, Verdict.OK)  # 1.0 -> 2.0

    controller.record("vpn-jp1", Lane.RYD, Verdict.THROTTLED)

    assert controller.window("vpn-jp1", Lane.YOUTUBE) == pytest.approx(2.0)
    assert controller.window("vpn-jp1", Lane.RYD) == pytest.approx(1.0)


def test_a_bot_check_makes_the_egress_unavailable_immediately() -> None:
    # One bot check is already a strong signal, and hitting the same address
    # again is precisely what turns a soft block into a hard one. So the
    # quarantine is immediate rather than waiting for a second strike.
    clock = FakeClock()
    controller = RateController(clock=clock)

    controller.record("vpn-jp1", Lane.YOUTUBE, Verdict.BLOCKED)

    assert controller.is_available("vpn-jp1", Lane.YOUTUBE) is False


def test_a_second_bot_check_backs_off_for_longer_than_the_first() -> None:
    # A fixed cooldown means an address that is genuinely burned gets retried
    # every five minutes forever, which is both useless and the behaviour most
    # likely to turn a temporary block into a permanent one.
    clock = FakeClock()
    controller = RateController(clock=clock)

    controller.record("vpn-jp1", Lane.YOUTUBE, Verdict.BLOCKED)
    clock.advance(300.0)
    assert controller.is_available("vpn-jp1", Lane.YOUTUBE) is True

    controller.record("vpn-jp1", Lane.YOUTUBE, Verdict.BLOCKED)
    clock.advance(300.0)

    assert controller.is_available("vpn-jp1", Lane.YOUTUBE) is False


def test_a_sustained_run_of_successes_resets_the_quarantine_streak() -> None:
    # Without this, an egress that was blocked twice last week comes out of its
    # next quarantine facing a twenty-minute cooldown it did nothing to earn,
    # and the pool slowly loses every route it ever had a bad hour with.
    clock = FakeClock()
    controller = RateController(clock=clock)
    controller.record("vpn-jp1", Lane.RYD, Verdict.BLOCKED)
    clock.advance(300.0)

    for _ in range(20):
        controller.record("vpn-jp1", Lane.RYD, Verdict.OK)

    controller.record("vpn-jp1", Lane.RYD, Verdict.BLOCKED)
    clock.advance(300.0)

    assert controller.is_available("vpn-jp1", Lane.RYD) is True
