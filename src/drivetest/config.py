"""The resolved run configuration and its default values, in one place.

This is the single home for defaults. The boundary (the CLI, or a future config
file) resolves a :class:`RunConfig` by starting from the defaults here and
applying overrides, then hands the finished config to the orchestrator - which
consumes it without knowing where any value came from. So no other module
imports these defaults; they flow in through ``RunConfig``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .thermal import ThermalPolicy
from .units import GIB

MINUTE = 60  # seconds in a minute

# Thermal thresholds sized for the USB *bridge's* drop point (~83 C observed on a
# passive enclosure), not the drive's own ~90 C warning: the ceiling leaves
# margin below the disconnect. See the ``thermal`` module for how they drive the
# pacing loops.
DEFAULT_THERMAL_POLICY = ThermalPolicy(
    ceiling_c=75,  # abort a running region at/above this
    cool_target_c=50,  # cool to this before each region
    start_max_c=50,  # refuse to start a region above this
    cool_max_wait_s=20 * MINUTE,  # never wait longer than this to cool
    poll_interval_s=5.0,  # temperature sampling cadence during a write
    cool_interval_s=20.0,  # sampling cadence while cooling
)

# --quick verifies just this leading span, for a fast sanity pass.
DEFAULT_QUICK_BYTES = 50 * GIB

# Regions a full write+verify is split into by default. 1 is a single continuous
# pass; raise it (e.g. 8) for a passive enclosure that would otherwise overheat,
# which pairs with the thermal policy to cool between regions.
DEFAULT_PARTS = 1


@dataclass(frozen=True)
class RunConfig:
    """A fully-resolved run configuration: what to do plus how to pace it.

    A pure structure with no built-in defaults - every field is set explicitly at
    construction. The CLI is the one place that resolves it, applying the defaults
    above; the orchestrator only ever consumes a finished config.
    """

    device: str
    write: bool
    quick: bool
    force: bool
    only: str | None
    assume_yes: bool
    log_dir: str | None
    parts: int
    quick_bytes: int
    policy: ThermalPolicy
