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

    def __post_init__(self) -> None:
        # The pacing loops rely on this ordering: cool below the start gate, and
        # start below the abort ceiling. A misconfigured policy (e.g. start_max
        # under cool_target) would otherwise wedge a region that can never start.
        if not (self.cool_target_c <= self.start_max_c < self.ceiling_c):
            raise ValueError(
                "thermal thresholds must satisfy "
                "cool_target_c <= start_max_c < ceiling_c, got "
                f"{self.cool_target_c}/{self.start_max_c}/{self.ceiling_c}"
            )
        # Positive intervals keep the cooldown loop making progress (a zero/negative
        # cool_interval_s would never advance the wait and could spin forever).
        if self.poll_interval_s <= 0 or self.cool_interval_s <= 0:
            raise ValueError("thermal poll/cool intervals must be positive")
        # Must be positive: a zero cap would skip the cooldown loop entirely and
        # return a nonsensical outcome (no sample taken, yet not "unreadable").
        if self.cool_max_wait_s <= 0:
            raise ValueError("cool_max_wait_s must be positive")


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


@dataclass(frozen=True)
class CooldownOutcome:
    """Result of a cooldown wait.

    ``last_temp`` is the last temperature actually observed during the wait. On a
    give-up (the cap elapsed while still hot) that sample may be up to one
    ``cool_interval_s`` old, since the loop sleeps after sampling; callers that
    need a fresh reading re-sample (as :meth:`ThermalController.prestart_ok` does).
    """

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
            # Cap the pause so cumulative waiting never exceeds cool_max_wait_s,
            # even when it is not a whole multiple of cool_interval_s.
            pause = min(policy.cool_interval_s, policy.cool_max_wait_s - waited)
            if temp is None:
                self._log("cooldown: temperature unreadable - pausing one interval")
                self._sleep(pause)
                return CooldownOutcome(
                    reached_target=False, unreadable=True, waited_s=waited + pause, last_temp=None
                )
            if temp <= policy.cool_target_c:
                self._log(f"cooldown: reached {temp} C after {waited:.0f}s")
                return CooldownOutcome(
                    reached_target=True, unreadable=False, waited_s=waited, last_temp=temp
                )
            self._sleep(pause)
            waited += pause
        self._log(f"cooldown: still {temp} C after {waited:.0f}s - continuing")
        return CooldownOutcome(
            reached_target=False, unreadable=False, waited_s=waited, last_temp=temp
        )

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
