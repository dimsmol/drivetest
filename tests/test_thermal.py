"""Tests for thermal pacing decisions and the cooldown/prestart loops."""

from __future__ import annotations

from dataclasses import replace

import pytest

from drivetest.config import DEFAULT_THERMAL_POLICY
from drivetest.thermal import (
    ThermalController,
    can_start,
    exceeds_ceiling,
    needs_cooldown,
)

from .conftest import collect_sleep

POLICY = DEFAULT_THERMAL_POLICY


def test_default_policy_satisfies_ordering_invariant():
    # The default keeps headroom between the cool target and the start gate (strict
    # <), so a small re-sample bump after cooling doesn't refuse to start a region.
    assert POLICY.cool_target_c < POLICY.start_max_c < POLICY.ceiling_c


def test_thermal_policy_rejects_bad_ordering():
    with pytest.raises(ValueError):
        replace(POLICY, ceiling_c=POLICY.start_max_c)  # ceiling not above start_max
    with pytest.raises(ValueError):
        replace(POLICY, start_max_c=POLICY.cool_target_c - 1)  # start below cool target


def test_thermal_policy_rejects_nonpositive_interval():
    with pytest.raises(ValueError):
        replace(POLICY, cool_interval_s=0)


def test_thermal_policy_rejects_nonpositive_poll_interval():
    with pytest.raises(ValueError):
        replace(POLICY, poll_interval_s=0)
    with pytest.raises(ValueError):
        replace(POLICY, poll_interval_s=-1)


def test_thermal_policy_rejects_nonpositive_max_wait():
    with pytest.raises(ValueError):
        replace(POLICY, cool_max_wait_s=0)

# Sample temps relative to the policy so these scenarios track threshold changes:
# HOT is above both gates (needs cooling, cannot start), COOL is below both.
HOT = max(POLICY.cool_target_c, POLICY.start_max_c) + 20
COOL = min(POLICY.cool_target_c, POLICY.start_max_c) - 5


def test_pure_thresholds():
    # Derived from POLICY so these track the configured thresholds, not literals.
    assert exceeds_ceiling(POLICY.ceiling_c, POLICY)       # at the ceiling trips
    assert exceeds_ceiling(POLICY.ceiling_c + 5, POLICY)
    assert not exceeds_ceiling(POLICY.ceiling_c - 1, POLICY)
    assert not exceeds_ceiling(None, POLICY)  # unknown never trips the ceiling

    assert needs_cooldown(POLICY.cool_target_c + 1, POLICY)  # above target -> cool
    assert not needs_cooldown(POLICY.cool_target_c, POLICY)
    assert not needs_cooldown(None, POLICY)

    assert can_start(POLICY.start_max_c, POLICY)             # at start-max is ok
    assert not can_start(POLICY.start_max_c + 1, POLICY)
    assert can_start(None, POLICY)  # unknown temperature -> allowed to start


def _controller(temps, policy=POLICY):
    """A controller reading a scripted sequence of temperatures."""
    seq = iter(temps)
    sleep, slept = collect_sleep()
    last = temps[-1] if temps else None
    ctrl = ThermalController(
        policy,
        read_temp=lambda: next(seq, last),
        sleep=sleep,
        log=lambda _m: None,
    )
    return ctrl, slept


def test_cooldown_stops_when_target_reached():
    ctrl, slept = _controller([HOT, HOT, COOL])
    outcome = ctrl.cooldown()
    assert outcome.reached_target
    assert outcome.last_temp == COOL
    # slept after each hot sample, not after reaching COOL
    assert len(slept) == 2
    # the reported wait is exactly the two hot intervals (no over/under-count)
    assert outcome.waited_s == 2 * POLICY.cool_interval_s


def test_cooldown_pauses_once_when_unreadable():
    ctrl, slept = _controller([None])
    outcome = ctrl.cooldown()
    assert outcome.unreadable
    assert not outcome.reached_target
    assert len(slept) == 1
    # The pause counts toward the reported wait (no under-reporting).
    assert outcome.waited_s == POLICY.cool_interval_s


def test_cooldown_unreadable_after_progress_counts_prior_wait():
    # Hot first (one interval slept while cooling), then unreadable: the reported
    # wait includes both the hot interval and the unreadable pause, no under-report.
    policy = replace(DEFAULT_THERMAL_POLICY, cool_max_wait_s=100, cool_interval_s=20)
    ctrl, slept = _controller([HOT, None], policy)
    outcome = ctrl.cooldown()
    assert outcome.unreadable
    assert not outcome.reached_target
    assert outcome.waited_s == 40
    assert slept == [20, 20]


def test_cooldown_gives_up_after_max_wait():
    # never cools; capped by cool_max_wait_s / cool_interval_s iterations
    policy = replace(DEFAULT_THERMAL_POLICY, cool_max_wait_s=60, cool_interval_s=20)
    ctrl, _slept = _controller([HOT] * 100, policy)
    outcome = ctrl.cooldown()
    assert not outcome.reached_target
    assert outcome.waited_s == 60  # 3 intervals of 20s
    assert outcome.last_temp == HOT  # the last observed (still-hot) sample


def test_cooldown_cap_smaller_than_interval_trims_the_single_pause():
    # cap 10 < interval 20: the one and only pause is trimmed to the whole cap, so
    # the loop still makes progress and reports exactly the cap, never overshooting.
    policy = replace(DEFAULT_THERMAL_POLICY, cool_max_wait_s=10, cool_interval_s=20)
    ctrl, slept = _controller([HOT] * 100, policy)
    outcome = ctrl.cooldown()
    assert not outcome.reached_target
    assert outcome.waited_s == 10
    assert slept == [10]


def test_cooldown_caps_total_wait_when_not_a_multiple():
    # cap 50 is not a whole multiple of interval 20: the last pause is trimmed to
    # 10 so total waiting is exactly 50, never overshooting to 60.
    policy = replace(DEFAULT_THERMAL_POLICY, cool_max_wait_s=50, cool_interval_s=20)
    ctrl, slept = _controller([HOT] * 100, policy)
    outcome = ctrl.cooldown()
    assert not outcome.reached_target
    assert outcome.waited_s == 50
    assert slept == [20, 20, 10]


def test_prestart_ok_when_already_cool():
    ctrl, _ = _controller([COOL])
    assert ctrl.prestart_ok()


def test_prestart_cools_then_proceeds():
    # starts hot, cools to COOL, then a final COOL sample -> can start
    ctrl, _ = _controller([HOT, HOT, COOL, COOL])
    assert ctrl.prestart_ok()


def test_prestart_proceeds_on_unknown_temperature():
    # Unknown temperature -> proceed without cooling (like the shell script); the
    # controller must not sleep or block on an unreadable sensor.
    ctrl, slept = _controller([None])
    assert ctrl.prestart_ok()
    assert slept == []


def test_prestart_refuses_when_still_hot_after_cooldown():
    # hot start, cooldown caps out still hot, final sample above start_max
    policy = replace(DEFAULT_THERMAL_POLICY, cool_max_wait_s=20, cool_interval_s=20)
    ctrl, _ = _controller([HOT, HOT, HOT], policy)
    assert not ctrl.prestart_ok()
