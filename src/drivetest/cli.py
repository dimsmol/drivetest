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
from pathlib import Path
from typing import NoReturn

from .config import DEFAULT_PARTS, DEFAULT_QUICK_BYTES, DEFAULT_THERMAL_POLICY, RunConfig
from .orchestrator import EXIT_REFUSED, RunContext, run
from .planning import parse_only_spec
from .units import GIB

PROG = "drivetest"

DESCRIPTION = "Health, integrity and performance test for an SSD/NVMe drive."

# Human label for the --quick span, derived from the constant so help text and
# the value stay in step.
QUICK_BYTES_LABEL = f"{DEFAULT_QUICK_BYTES // GIB}G"

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


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that exits usage errors with ``EXIT_REFUSED``.

    argparse's default is exit code 2, which collides with ``EXIT_ATTENTION`` (a
    run that completed but flagged an issue). A bad invocation is really "refused
    to run", so map it onto ``EXIT_REFUSED`` (1) - the same bucket as a safety
    refusal - keeping ``2`` to mean "ran, needs attention". ``--help`` still
    exits 0 (argparse calls ``exit`` directly for that, not ``error``).
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_REFUSED, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
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
        default=None,  # None = not given; resolved to DEFAULT_PARTS after validation
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

    # --parts defaults to None so an explicit value is distinguishable from the
    # default: the "requires --write" rule then applies to any explicit --parts,
    # not just one that happens to differ from DEFAULT_PARTS (which would silently
    # accept "--parts 1", and break outright if the default ever changed).
    parts = DEFAULT_PARTS if ns.parts is None else ns.parts
    if parts < 1:
        parser.error("--parts needs a positive integer")
    if ns.parts is not None:
        if not ns.write:
            parser.error("--parts only applies together with --write")
        if ns.quick:
            parser.error("--parts has no effect with --quick (a quick run writes one region)")
    if ns.only is not None:
        if not (ns.write and not ns.quick):
            parser.error("--only requires --write without --quick")
        try:
            parse_only_spec(ns.only, parts)
        except ValueError as exc:
            parser.error(str(exc))
    if ns.quick and not ns.write:
        parser.error("--quick only applies together with --write")
    if ns.force and not ns.write:
        parser.error("--force only applies together with --write")

    # The one place a run config is assembled: parsed flags plus the defaults for
    # the knobs with no flag (quick_bytes, thermal policy).
    return RunConfig(
        device=ns.device,
        write=ns.write,
        quick=ns.quick,
        force=ns.force,
        only=ns.only,
        assume_yes=ns.assume_yes,
        log_dir=Path(ns.log_dir) if ns.log_dir else None,
        parts=parts,
        quick_bytes=DEFAULT_QUICK_BYTES,
        policy=DEFAULT_THERMAL_POLICY,
    )


def main(argv: list[str] | None = None) -> int:
    options = parse_args(sys.argv[1:] if argv is None else argv)

    if os.geteuid() != 0:
        print("error: run as root (sudo)", file=sys.stderr)
        return EXIT_REFUSED

    # Resolve --log-dir into the run's working directory (where the timestamped
    # log folder is created); default to the current directory.
    return run(options, RunContext(workdir=options.log_dir or Path(".")))


if __name__ == "__main__":
    raise SystemExit(main())
