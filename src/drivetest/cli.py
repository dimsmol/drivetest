"""Command-line interface: parse arguments into a validated :class:`Options`.

``parse_args`` is pure (takes an argv list, returns Options or raises
SystemExit), so option validation is unit-tested without running anything.
``main`` wires Options to the orchestrator.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from .planning import parse_only_spec

PROG = "drivetest"

DESCRIPTION = "Health, integrity and performance test for an SSD/NVMe drive."

EPILOG = """\
examples:
  sudo drivetest /dev/sdb                         health + read benchmarks only
  sudo drivetest --write /dev/sdb                 + full destructive write+verify
  sudo drivetest --write --quick /dev/sdb         + verify only the first 50G
  sudo drivetest --write --parts 8 /dev/sdb       paced full write for a passive enclosure
  sudo drivetest --write --parts 8 --only 1-4 /dev/sdb   first half now
  sudo drivetest --write --parts 8 --only 5-8 /dev/sdb   the rest later

Region boundaries depend on --parts, so pass the SAME --parts N when resuming
with --only.
"""


@dataclass(frozen=True)
class Options:
    device: str
    write: bool = False
    quick: bool = False
    force: bool = False
    parts: int = 1
    only: str | None = None
    assume_yes: bool = False
    log_dir: str | None = None


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
        help="with --write, verify only the first 50G (fast sanity check)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow --write to a non-blank disk (has partitions/signatures)",
    )
    parser.add_argument(
        "--parts",
        type=int,
        default=1,
        metavar="N",
        help="split the full write+verify into N cooled regions (default 1)",
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


def parse_args(argv: list[str]) -> Options:
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

    return Options(
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
    from .orchestrator import RunContext, run

    options = parse_args(sys.argv[1:] if argv is None else argv)

    if os.geteuid() != 0:
        print("error: run as root (sudo)", file=sys.stderr)
        return 1

    return run(options, RunContext())


if __name__ == "__main__":
    raise SystemExit(main())
