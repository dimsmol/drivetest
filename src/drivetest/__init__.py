"""drivetest - health, integrity and performance test for a storage device.

The package is deliberately split so each module is useful on its own, with the
heavy logic (parsing, safety decisions, region math, result classification) in
pure functions that unit-test against fixtures without touching real hardware.
See the Development section of the README for the per-module breakdown.
"""

__version__ = "0.1.0"
