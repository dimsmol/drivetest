"""Thermal pacing for passively-cooled USB enclosures.

A fanless enclosure (e.g. the Asus ROG Strix Arion) overheats on a sustained
full-drive write and the USB *bridge* drops off the bus - not the drive, which
is happy well past that point. So we pace: cool before each region, refuse to
start hot, and abort a region cleanly at a ceiling before a hard disconnect.

The decision logic is pure functions; :class:`ThermalController` runs the wait
loops with an injected clock/sleep/temperature source so tests exercise the
loops without real time passing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# A temperature reading of ``None`` means "could not read" (flaky bridge). The
# policy treats an unknown temperature conservatively but never *blocks* on it:
# unreadable -> proceed, but pause on cooldown.
Temp = int | None


@dataclass(frozen=True)
class ThermalPolicy:
    """Tunable thermal thresholds (Celsius; waits/cadences in seconds).

    A pure data structure with no built-in defaults - the canonical values live
    in :mod:`drivetest.config` as ``DEFAULT_THERMAL_POLICY``, where each field's
    meaning is documented.
    """

    ceiling_c: int
    cool_target_c: int
    start_max_c: int
    cool_max_wait_s: int
    poll_interval_s: float
    cool_interval_s: float


def exceeds_ceiling(temp: Temp, policy: ThermalPolicy) -> bool:
    """True only when a real reading is at/above the ceiling."""
    return temp is not None and temp >= policy.ceiling_c


def needs_cooldown(temp: Temp, policy: ThermalPolicy) -> bool:
    """True when a real reading is above the cool target (so cool first)."""
    return temp is not None and temp > policy.cool_target_c


def can_start(temp: Temp, policy: ThermalPolicy) -> bool:
    """Whether a region may start. Unknown temperature -> allow (like the shell
    script); a known reading must be at/below ``start_max_c``.
    """
    return temp is None or temp <= policy.start_max_c


Sample = Callable[[Temp], None]


def _ignore_sample(_temp: Temp) -> None:
    pass


def _ignore_log(_message: str) -> None:
    pass


@dataclass
class CooldownOutcome:
    reached_target: bool
    unreadable: bool
    waited_s: float
    last_temp: Temp


class ThermalController:
    """Runs cooldown/pre-start waits using injected effects.

    ``read_temp`` returns the current temperature (or None), ``sleep`` advances
    time, and ``on_sample``/``log`` are optional observers. Nothing here calls
    the wall clock directly, so tests are deterministic.
    """

    def __init__(
        self,
        policy: ThermalPolicy,
        read_temp: Callable[[], Temp],
        *,
        sleep: Callable[[float], None],
        on_sample: Sample | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.policy = policy
        self._read_temp = read_temp
        self._sleep = sleep
        self._on_sample = on_sample or _ignore_sample
        self._log = log or _ignore_log

    def _sample(self) -> Temp:
        temp = self._read_temp()
        self._on_sample(temp)
        return temp

    def cooldown(self) -> CooldownOutcome:
        """Idle until the drive cools to the target, or the wait cap elapses.

        If temperature is unreadable, pause once for the cool interval and
        return (we can neither confirm nor deny it is cool).
        """
        policy = self.policy
        self._log(
            f"cooldown: waiting for <= {policy.cool_target_c} C (max {policy.cool_max_wait_s}s)"
        )
        waited = 0.0
        temp: Temp = None
        while waited < policy.cool_max_wait_s:
            temp = self._sample()
            if temp is None:
                self._log("cooldown: temperature unreadable - pausing one interval")
                self._sleep(policy.cool_interval_s)
                return CooldownOutcome(False, True, waited, None)
            if temp <= policy.cool_target_c:
                self._log(f"cooldown: reached {temp} C after {waited:.0f}s")
                return CooldownOutcome(True, False, waited, temp)
            self._sleep(policy.cool_interval_s)
            waited += policy.cool_interval_s
        self._log(f"cooldown: still {temp} C after {waited:.0f}s - continuing")
        return CooldownOutcome(False, False, waited, temp)

    def prestart_ok(self) -> bool:
        """Gate before a region: if hot, cool first; then refuse if still too
        hot to start. Returns True to proceed.
        """
        temp = self._sample()
        if needs_cooldown(temp, self.policy):
            self._log(f"drive at {temp} C before start - cooling first")
            self.cooldown()
            temp = self._sample()
        if not can_start(temp, self.policy):
            self._log(
                f"refusing to start: {temp} C still above {self.policy.start_max_c} C "
                "(improve cooling/ambient, then resume)"
            )
            return False
        return True
