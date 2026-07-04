"""Command-line interface: resolve arguments into a :class:`RunConfig`.

This is the boundary that decides the run: it starts from the defaults in
:mod:`drivetest.config` and overrides them with the parsed flags, then hands the
finished config to the orchestrator. ``parse_args`` is pure (takes an argv list,
returns RunConfig or raises SystemExit), so validation is unit-tested without
running anything.
"""

from __future__ import annotations

import argparse
import os
import sys

from .config import DEFAULT_PARTS, QUICK_BYTES, RunConfig
from .orchestrator import RunContext, run
from .planning import parse_only_spec
from .units import GIB

PROG = "drivetest"

DESCRIPTION = "Health, integrity and performance test for an SSD/NVMe drive."

# Human label for the --quick span, derived from the constant so help text and
# the value stay in step.
QUICK_BYTES_LABEL = f"{QUICK_BYTES // GIB}G"

EPILOG = f"""\
examples:
  sudo drivetest /dev/sdb                         health + read benchmarks only
  sudo drivetest --write /dev/sdb                 + full destructive write+verify
  sudo drivetest --write --quick /dev/sdb         + verify only the first {QUICK_BYTES_LABEL}
  sudo drivetest --write --parts 8 /dev/sdb       paced full write for a passive enclosure
  sudo drivetest --write --parts 8 --only 1-4 /dev/sdb   first half now
  sudo drivetest --write --parts 8 --only 5-8 /dev/sdb   the rest later

Region boundaries depend on --parts, so pass the SAME --parts N when resuming
with --only.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("device", help="target block device, e.g. /dev/sdb or /dev/nvme0n1")
    parser.add_argument(
        "--write",
        action="store_true",
        help="also run the destructive write+verify pass (WIPES the target)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help=f"with --write, verify only the first {QUICK_BYTES_LABEL} (fast sanity check)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow --write to a non-blank disk (has partitions/signatures)",
    )
    parser.add_argument(
        "--parts",
        type=int,
        default=DEFAULT_PARTS,
        metavar="N",
        help=f"split the full write+verify into N cooled regions (default {DEFAULT_PARTS})",
    )
    parser.add_argument(
        "--only",
        metavar="SPEC",
        help="run only some of the N parts, e.g. '1-4', '5', '1-3,7' (to resume)",
    )
    parser.add_argument(
        "--assume-yes",
        action="store_true",
        help="skip the interactive serial confirmation (non-interactive use)",
    )
    parser.add_argument(
        "--log-dir",
        metavar="DIR",
        help="parent directory for the timestamped log folder (default: cwd)",
    )
    return parser


def parse_args(argv: list[str]) -> RunConfig:
    """Parse and validate argv. Exits (SystemExit) on any invalid combination."""
    parser = build_parser()
    ns = parser.parse_args(argv)

    if ns.parts < 1:
        parser.error("--parts needs a positive integer")
    if ns.only is not None:
        if not (ns.write and not ns.quick):
            parser.error("--only requires --write without --quick")
        try:
            parse_only_spec(ns.only, ns.parts)
        except ValueError as exc:
            parser.error(str(exc))
    if ns.quick and not ns.write:
        parser.error("--quick only applies together with --write")
    if ns.force and not ns.write:
        parser.error("--force only applies together with --write")

    # Start from the defaults baked into RunConfig and override with the parsed
    # flags; unset knobs (quick_bytes, thermal policy) keep their defaults.
    return RunConfig(
        device=ns.device,
        write=ns.write,
        quick=ns.quick,
        force=ns.force,
        parts=ns.parts,
        only=ns.only,
        assume_yes=ns.assume_yes,
        log_dir=ns.log_dir,
    )


def main(argv: list[str] | None = None) -> int:
    options = parse_args(sys.argv[1:] if argv is None else argv)

    if os.geteuid() != 0:
        print("error: run as root (sudo)", file=sys.stderr)
        return 1

    return run(options, RunContext())


if __name__ == "__main__":
    raise SystemExit(main())
