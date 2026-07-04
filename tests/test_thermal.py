"""Tests for thermal pacing decisions and the cooldown/prestart loops."""

from __future__ import annotations

from dataclasses import replace

from drivetest.config import DEFAULT_THERMAL_POLICY
from drivetest.thermal import (
    ThermalController,
    can_start,
    exceeds_ceiling,
    needs_cooldown,
)

from .conftest import collect_sleep

POLICY = DEFAULT_THERMAL_POLICY


def test_pure_thresholds():
    assert exceeds_ceiling(78, POLICY)
    assert exceeds_ceiling(90, POLICY)
    assert not exceeds_ceiling(77, POLICY)
    assert not exceeds_ceiling(None, POLICY)  # unknown never trips the ceiling

    assert needs_cooldown(51, POLICY)
    assert not needs_cooldown(50, POLICY)
    assert not needs_cooldown(None, POLICY)

    assert can_start(55, POLICY)
    assert not can_start(56, POLICY)
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
    ctrl, slept = _controller([70, 60, 48])
    outcome = ctrl.cooldown()
    assert outcome.reached_target
    assert outcome.last_temp == 48
    # slept twice (after 70 and after 60), not after reaching 48
    assert len(slept) == 2


def test_cooldown_pauses_once_when_unreadable():
    ctrl, slept = _controller([None])
    outcome = ctrl.cooldown()
    assert outcome.unreadable
    assert not outcome.reached_target
    assert len(slept) == 1


def test_cooldown_gives_up_after_max_wait():
    # never cools; capped by cool_max_wait_s / cool_interval_s iterations
    policy = replace(DEFAULT_THERMAL_POLICY, cool_max_wait_s=60, cool_interval_s=20)
    ctrl, _slept = _controller([80] * 100, policy)
    outcome = ctrl.cooldown()
    assert not outcome.reached_target
    assert outcome.waited_s == 60  # 3 intervals of 20s


def test_prestart_ok_when_already_cool():
    ctrl, _ = _controller([40])
    assert ctrl.prestart_ok()


def test_prestart_cools_then_proceeds():
    # starts hot (70), cools to 45, then a final sample at 45 -> can start
    ctrl, _ = _controller([70, 60, 45, 45])
    assert ctrl.prestart_ok()


def test_prestart_refuses_when_still_hot_after_cooldown():
    # hot start, cooldown caps out still hot, final sample above start_max
    policy = replace(DEFAULT_THERMAL_POLICY, cool_max_wait_s=20, cool_interval_s=20)
    ctrl, _ = _controller([70, 70, 70], policy)
    assert not ctrl.prestart_ok()
