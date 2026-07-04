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
    assert POLICY.cool_target_c <= POLICY.start_max_c < POLICY.ceiling_c


def test_thermal_policy_rejects_bad_ordering():
    with pytest.raises(ValueError):
        replace(POLICY, ceiling_c=POLICY.start_max_c)  # ceiling not above start_max
    with pytest.raises(ValueError):
        replace(POLICY, start_max_c=POLICY.cool_target_c - 1)  # start below cool target


def test_thermal_policy_rejects_nonpositive_interval():
    with pytest.raises(ValueError):
        replace(POLICY, cool_interval_s=0)

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
    )
    return ctrl, slept


def test_cooldown_stops_when_target_reached():
    ctrl, slept = _controller([HOT, HOT, COOL])
    outcome = ctrl.cooldown()
    assert outcome.reached_target
    assert outcome.last_temp == COOL
    # slept after each hot sample, not after reaching COOL
    assert len(slept) == 2


def test_cooldown_pauses_once_when_unreadable():
    ctrl, slept = _controller([None])
    outcome = ctrl.cooldown()
    assert outcome.unreadable
    assert not outcome.reached_target
    assert len(slept) == 1
    # The pause counts toward the reported wait (no under-reporting).
    assert outcome.waited_s == POLICY.cool_interval_s


def test_cooldown_gives_up_after_max_wait():
    # never cools; capped by cool_max_wait_s / cool_interval_s iterations
    policy = replace(DEFAULT_THERMAL_POLICY, cool_max_wait_s=60, cool_interval_s=20)
    ctrl, _slept = _controller([HOT] * 100, policy)
    outcome = ctrl.cooldown()
    assert not outcome.reached_target
    assert outcome.waited_s == 60  # 3 intervals of 20s


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


def test_prestart_refuses_when_still_hot_after_cooldown():
    # hot start, cooldown caps out still hot, final sample above start_max
    policy = replace(DEFAULT_THERMAL_POLICY, cool_max_wait_s=20, cool_interval_s=20)
    ctrl, _ = _controller([HOT, HOT, HOT], policy)
    assert not ctrl.prestart_ok()
