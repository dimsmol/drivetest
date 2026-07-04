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

# Thermal thresholds sized for the USB *bridge's* drop point (~83 C observed on a
# passive enclosure), not the drive's own ~90 C warning: the ceiling leaves
# margin below the disconnect. See the ``thermal`` module for how they drive the
# pacing loops.
DEFAULT_THERMAL_POLICY = ThermalPolicy(
    ceiling_c=78,           # abort a running region at/above this
    cool_target_c=50,       # cool to this before each region
    start_max_c=55,         # refuse to start a region above this
    cool_max_wait_s=1200,   # never wait longer than this to cool
    poll_interval_s=5.0,    # temperature sampling cadence during a write
    cool_interval_s=20.0,   # sampling cadence while cooling
)

# --quick verifies just this leading span, for a fast sanity pass.
QUICK_BYTES = 50 * GIB

# Regions a full write+verify is split into by default. 1 is a single continuous
# pass; raise it (e.g. 8) for a passive enclosure that would otherwise overheat,
# which pairs with the thermal policy to cool between regions.
DEFAULT_PARTS = 1


@dataclass(frozen=True)
class RunConfig:
    """A fully-resolved run configuration: what to do plus how to pace it.

    Produced at the boundary by overriding the defaults below, and consumed by
    the orchestrator, which never reads the raw defaults itself. ``ThermalPolicy``
    is immutable, so sharing one default instance as a field default is safe.
    """

    device: str
    write: bool = False
    quick: bool = False
    force: bool = False
    only: str | None = None
    assume_yes: bool = False
    log_dir: str | None = None
    parts: int = DEFAULT_PARTS
    quick_bytes: int = QUICK_BYTES
    policy: ThermalPolicy = DEFAULT_THERMAL_POLICY
