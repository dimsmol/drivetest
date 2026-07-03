"""End-to-end run: safety -> SMART baseline -> write+verify -> benchmarks ->
SMART diff -> summary.

This is the thin glue on top of the (heavily tested) pure modules. Its external
effects are injected via :class:`RunContext`, so the safety-abort paths and the
read-only happy path can be integration-tested with fakes.

Exit codes: ``0`` OK, ``1`` usage/safety refusal, ``2`` attention needed
(verify FAIL/OVERHEAT, disconnect, or worsened SMART).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

from . import smart
from .devices import Device, all_serials, find_device, list_devices
from .fio import (
    FioRunner,
    PopenFactory,
    RegionResult,
    build_read_argv,
    default_popen,
    parse_read_json,
)
from .planning import parse_only_spec, plan_regions, quick_region
from .probe import gather_blank_probe, gather_root_info
from .proc import Runner, SubprocessRunner
from .report import Logger, SmartVerdict, classify_smart, diff_smart, format_gib
from .safety import (
    blocking_failures,
    check_identity_stable,
    check_not_mounted,
    evaluate_write_safety,
)
from .smart import SmartInfo
from .thermal import ThermalController, ThermalPolicy
from .tools import missing_tools, required_tools

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ATTENTION = 2


@dataclass(frozen=True)
class Options:
    """A validated run configuration (produced by the CLI, consumed here)."""

    device: str
    write: bool = False
    quick: bool = False
    force: bool = False
    parts: int = 1
    only: str | None = None
    assume_yes: bool = False
    log_dir: str | None = None


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


@dataclass
class RunContext:
    """Injected effects for a run (all have production defaults)."""

    runner: Runner = field(default_factory=SubprocessRunner)
    policy: ThermalPolicy = field(default_factory=ThermalPolicy)
    workdir: Path = field(default_factory=lambda: Path("."))
    sleep: Callable[[float], None] = time.sleep
    popen: PopenFactory = default_popen
    confirm: Callable[[str], str] = input
    stream: TextIO | None = None
    stamp: str | None = None


def run(options: Options, ctx: RunContext | None = None) -> int:
    ctx = ctx or RunContext()
    runner = ctx.runner

    # --- required tools ---------------------------------------------------
    missing = missing_tools(required_tools(options.device))
    if missing:
        print(f"error: missing required tools: {' '.join(missing)}")
        print("install them, e.g.: nix-shell -p fio smartmontools nvme-cli usbutils")
        return EXIT_REFUSED

    # --- resolve the device ----------------------------------------------
    try:
        dev = find_device(runner, options.device)
    except LookupError as exc:
        print(f"error: {exc}")
        return EXIT_REFUSED

    mode = smart.detect_access_mode(runner, dev.path)

    # --- log folder -------------------------------------------------------
    stamp = ctx.stamp or _timestamp()
    log_dir = ctx.workdir / f"drive_test_{dev.serial or 'unknown'}_{stamp}"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = Logger(log_dir / "summary.log", stream=ctx.stream)

    logger.log(f"== drivetest {stamp} ==")
    logger.log(f"device : {dev.path}")
    logger.log(f"model  : {dev.model}")
    logger.log(f"serial : {dev.serial}")
    logger.log(f"size   : {format_gib(dev.size)} ({dev.size} B)")
    logger.log(f"bus    : {dev.tran}")
    logger.log(f"smart  : smartctl {' '.join(mode) or '(auto)'}")
    logger.log(f"logs   : {log_dir}/")
    logger.log(f"mode   : {'READ + DESTRUCTIVE WRITE/VERIFY' if options.write else 'read-only'}")
    logger.log("")

    # --- safety + confirmation (write only) ------------------------------
    if options.write:
        rc = _guard_and_confirm(options, ctx, dev, logger)
        if rc != EXIT_OK:
            return rc

    # --- temperature source shared by thermal + fio monitor --------------
    temps: list[int] = []

    def read_temp():
        t = smart.read_temperature(runner, dev.path, mode)
        if t is not None:
            temps.append(t)
        return t

    thermal = ThermalController(
        ctx.policy, read_temp, sleep=ctx.sleep, log=lambda m: logger.log(f"   {m}")
    )
    fio_runner = FioRunner(
        read_temp=read_temp,
        policy=ctx.policy,
        sleep=ctx.sleep,
        popen=ctx.popen,
        # Live fio progress to the console; full output is also saved to the log
        # file by the runner's drain thread.
        echo=lambda line: print(line, end="", file=ctx.stream or sys.stdout),
    )

    # --- SMART baseline ---------------------------------------------------
    before = _smart_snapshot(runner, dev, mode, log_dir / "smart_before.txt")
    logger.log(">> SMART baseline")
    logger.log(f"   health: {_health_str(before)}")
    logger.log(f"   temp  : {read_temp()} C")
    logger.log("")

    # --- write + verify ---------------------------------------------------
    verify = "skipped"
    if options.write:
        verify = _write_phase(options, ctx, dev, logger, thermal, fio_runner, log_dir)
        logger.log(f"   write/verify: {verify}")
        logger.log("")

    # --- did the device survive? -----------------------------------------
    dev_gone = options.write and not _device_present(runner, dev)
    if dev_gone:
        logger.log(f"!! {dev.path} is gone or changed identity since the write started.")
        logger.log("   It dropped off the bus (commonly a thermal disconnect on a passive")
        logger.log("   USB enclosure). Skipping read benchmarks and post-SMART. Let it cool,")
        logger.log("   replug, and resume with --only <remaining parts>.")
        logger.log("")

    # --- read benchmarks --------------------------------------------------
    if not dev_gone:
        _read_benchmarks(runner, dev, logger, log_dir)

    # --- SMART after + diff ----------------------------------------------
    after = _smart_after(runner, dev, mode, log_dir / "smart_after.txt", dev_gone)
    deltas = diff_smart(before, after)
    verdict = classify_smart(after, deltas)

    # --- summary ----------------------------------------------------------
    peak = max(temps) if temps else None
    logger.log("== summary ==")
    logger.log(f"write/verify : {verify}")
    logger.log(f"peak temp    : {peak} C")
    logger.log(f"SMART diff   : {verdict.value}")
    for d in deltas:
        logger.log(f"   {d.field}: {d.before} -> {d.after}")
    logger.log(f"full logs    : {log_dir}/")
    logger.log("")

    bad_verify = verify in (RegionResult.FAIL.value, RegionResult.OVERHEAT.value)
    if dev_gone or bad_verify or verdict is not SmartVerdict.CLEAN:
        if dev_gone:
            logger.log("RESULT: INCOMPLETE - device disconnected mid-run (likely thermal). "
                       "Cool it, replug, and resume with --only.")
        elif verify == RegionResult.OVERHEAT.value:
            logger.log("RESULT: INCOMPLETE - stopped on temperature ceiling; "
                       "use more --parts (and/or a fan).")
        else:
            logger.log("RESULT: ATTENTION NEEDED - inspect the logs above.")
        return EXIT_ATTENTION

    logger.log("RESULT: OK")
    return EXIT_OK


# --- helpers --------------------------------------------------------------


def _guard_and_confirm(options: Options, ctx: RunContext, dev: Device, logger: Logger) -> int:
    """Run the pre-write guards, print failures, and take confirmation."""
    root = gather_root_info(ctx.runner)
    probe = gather_blank_probe(ctx.runner, dev)
    serials = all_serials(list_devices(ctx.runner))

    checks = evaluate_write_safety(
        dev, root=root, probe=probe, all_serials=serials, force=options.force
    )
    for check in checks:
        marker = "ok" if check.ok else "REFUSE"
        logger.log(f"   [{marker}] {check.name}: {check.detail}")
    failures = blocking_failures(checks)
    if failures:
        logger.log("")
        logger.log("error: refusing to write. Failed guard(s): "
                   + ", ".join(c.name for c in failures))
        return EXIT_REFUSED

    logger.log("")
    logger.log("*** WRITE mode will ERASE ALL DATA on:")
    logger.log(f"      {dev.path} | {dev.model} | serial {dev.serial} | "
               f"{format_gib(dev.size)} | bus {dev.tran}")
    logger.log("")

    if not options.assume_yes:
        answer = ctx.confirm(f"Type the serial ({dev.serial}) to confirm: ")
        if answer.strip() != dev.serial or not dev.serial:
            logger.log("aborted (serial mismatch or empty).")
            return EXIT_REFUSED

    # Last-line-of-defense re-check just before writing: same device, still idle.
    try:
        current = find_device(ctx.runner, dev.path)
    except LookupError:
        logger.log("error: device vanished after confirmation - aborting write.")
        return EXIT_REFUSED
    ident = check_identity_stable(dev.identity, current.identity)
    if not ident.ok:
        logger.log(f"error: {ident.detail} - aborting write.")
        return EXIT_REFUSED
    mount = check_not_mounted(current)
    if not mount.ok:
        logger.log(f"error: {mount.detail} - aborting write.")
        return EXIT_REFUSED
    return EXIT_OK


def _write_phase(
    options: Options, ctx: RunContext, dev: Device, logger: Logger,
    thermal: ThermalController, fio_runner: FioRunner, log_dir: Path,
) -> str:
    """Run the quick or paced full write+verify. Returns the verify result string."""
    if options.quick:
        region = quick_region()
        logger.log(f">> write+verify (crc32c, first {format_gib(region.size)} quick); "
                   f"ceiling {ctx.policy.ceiling_c} C")
        if thermal.prestart_ok():
            result = fio_runner.run_region(dev.path, region, log_dir / "fio_writeverify.log")
        else:
            result = RegionResult.OVERHEAT
        logger.log(f"   result: {result.value}")
        return result.value

    regions = plan_regions(dev.size, options.parts)
    selected = parse_only_spec(options.only, options.parts) if options.only else None
    sel_desc = f"parts {options.only}" if options.only else "all parts"
    logger.log(
        f">> write+verify (crc32c, full) in {options.parts} part(s), running {sel_desc}; "
        f"ceiling {ctx.policy.ceiling_c} C, cool to {ctx.policy.cool_target_c} C before each"
    )

    verify = "PASS"
    ran = 0
    for region in regions:
        if selected is not None and region.index not in selected:
            logger.log(f">> part {region.index}/{options.parts}: skipped (not selected)")
            continue
        logger.log(f">> part {region.index}/{options.parts}  "
                   f"offset={format_gib(region.offset)}  size={format_gib(region.size)}")
        if not thermal.prestart_ok():
            verify = RegionResult.OVERHEAT.value
            logger.log(f"   stopping before part {region.index} (too hot to start)")
            break
        result = fio_runner.run_region(
            dev.path, region, log_dir / f"fio_writeverify_part{region.index}.log"
        )
        ran += 1
        logger.log(f"   part {region.index}: {result.value}")
        if result is not RegionResult.PASS:
            verify = result.value
            logger.log(f"   stopping after part {region.index} ({result.value})")
            break

    if verify == "PASS" and options.only:
        verify = f"PASS (parts {options.only} of {options.parts} - not the whole drive)"
    if ran == 0:
        logger.log("   note: no parts ran")
    return verify


def _device_present(runner: Runner, dev: Device) -> bool:
    """True if the target is still the same physical device we started on."""
    try:
        current = find_device(runner, dev.path)
    except LookupError:
        return False
    return current.identity == dev.identity


def _smart_snapshot(runner: Runner, dev: Device, mode: list[str], text_path: Path) -> SmartInfo:
    """Save the raw ``smartctl -x`` text and return a parsed snapshot."""
    text = runner.run(["smartctl", "-x", *mode, dev.path])
    text_path.write_text(text.stdout or text.stderr)
    return smart.read_smart(runner, dev.path, mode)


def _smart_after(
    runner: Runner, dev: Device, mode: list[str], text_path: Path, dev_gone: bool
) -> SmartInfo:
    if dev_gone:
        text_path.write_text("device not present after run (disconnected) - SMART not read\n")
        return SmartInfo(raw=None)
    return _smart_snapshot(runner, dev, mode, text_path)


def _read_benchmarks(runner: Runner, dev: Device, logger: Logger, log_dir: Path) -> None:
    for kind, label in (("seqread", "sequential read (1M, qd32, 60s)"),
                        ("randread", "random read (4k, qd64, 30s)")):
        logger.log(f">> {label}")
        result = runner.run(build_read_argv(dev.path, kind))
        (log_dir / f"fio_{kind}.json").write_text(result.stdout)
        try:
            stats = parse_read_json(result.json(), kind)
        except ValueError:
            logger.log("   (could not parse fio output)")
            continue
        if kind == "seqread":
            logger.log(f"   bandwidth: {stats.bw_mb:.0f} MB/s")
        else:
            logger.log(f"   IOPS: {stats.iops:.0f} ({stats.bw_mb:.0f} MB/s)")
    logger.log("")


def _health_str(info: SmartInfo) -> str:
    if not info.has_report:
        return "?"
    passed = {True: "PASSED", False: "FAILED", None: "?"}[info.health_passed]
    return f"{passed} (temp {info.temperature_c} C, media_errors {info.media_errors})"
