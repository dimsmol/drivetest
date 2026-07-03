"""drivetest - health, integrity and performance test for a storage device.

The package is deliberately split so each module is useful on its own:

- ``proc``      - a thin, mockable subprocess seam (run a command, parse JSON).
- ``tools``     - check that required external CLIs are present.
- ``devices``   - enumerate block devices and model their identity (``lsblk``).
- ``safety``    - the destructive-write guards (the crown jewel; all pure).
- ``smart``     - read SMART/health/temperature via ``smartctl``/``nvme``.
- ``thermal``   - thermal pacing policy for passively-cooled enclosures.
- ``planning``  - split a drive into regions and parse ``--only`` specs (pure).
- ``fio``       - build and run fio write+verify and read benchmarks.
- ``report``    - summary, SMART diff and result classification.
- ``cli`` / ``orchestrator`` - argument parsing and the end-to-end run.

The heavy logic (parsing, safety decisions, region math, result
classification) lives in pure functions so it can be unit-tested against
fixtures without touching real hardware.
"""

__version__ = "0.1.0"
