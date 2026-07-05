"""End-to-end run: safety -> SMART baseline -> write+verify -> benchmarks ->
SMART diff -> summary.

This is the thin glue on top of the (heavily tested) pure modules. Its external
effects are injected via :class:`RunContext`, so the safety-abort paths and the
read-only happy path can be integration-tested with fakes.

Exit codes: ``0`` OK, ``1`` refused to run (a usage error or a safety guard, so
nothing was written), ``2`` ran but needs attention (verify FAIL/OVERHEAT,
disconnect, or worsened SMART). A passing ``--only`` subset also exits ``0``: it
verified only the parts it ran, so the whole drive is confirmed only once every
part has passed across runs - the summary spells this out ("not the whole
drive"), since the exit code alone can't distinguish a subset pass from a full one.
"""

from __future__ import annotations

import contextlib
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

from . import smart
from .config import RunConfig
from .devices import Device, all_serials, find_device, list_devices
from .fio import (
    FioReadError,
    FioRunner,
    PopenFactory,
    ReadKind,
    RegionResult,
    build_read_argv,
    default_popen,
    parse_read_json,
)
from .planning import parse_only_spec, plan_regions, quick_region
from .probe import gather_blank_probe, gather_root_info
from .proc import Runner, SubprocessRunner
from .report import (
    Logger,
    SmartVerdict,
    VerifyOutcome,
    VerifyStatus,
    classify_smart,
    describe_verdict,
    diff_smart,
    format_gib,
    health_regressions,
)
from .safety import (
    blocking_failures,
    check_identity_stable,
    check_not_mounted,
    evaluate_write_safety,
)
from .smart import SmartInfo
from .thermal import ThermalController
from .tools import missing_tools, required_tools

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ATTENTION = 2

# Explicit bridge from a fio region result to a write-phase status, so the two
# enums stay independent (no reliance on shared values). Exhaustiveness is tested.
_REGION_TO_VERIFY: dict[RegionResult, VerifyStatus] = {
    RegionResult.PASS: VerifyStatus.PASS,
    RegionResult.FAIL: VerifyStatus.FAIL,
    RegionResult.OVERHEAT: VerifyStatus.OVERHEAT,
}


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


@dataclass
class RunContext:
    """Injected effects for a run (all have production defaults).

    Effects only - the configuration (including the thermal policy) travels
    separately in :class:`~drivetest.config.RunConfig`.
    """

    runner: Runner = field(default_factory=SubprocessRunner)
    workdir: Path = field(default_factory=lambda: Path("."))
    sleep: Callable[[float], None] = time.sleep
    popen: PopenFactory = default_popen
    confirm: Callable[[str], str] = input
    stream: TextIO | None = None
    stamp: str | None = None
    sys_block: str = "/sys/block"


def run(config: RunConfig, ctx: RunContext) -> int:
    runner = ctx.runner

    # --- required tools ---------------------------------------------------
    missing = missing_tools(required_tools(config.device))
    if missing:
        print(f"error: missing required tools: {' '.join(missing)}")
        print("install them, e.g.: nix-shell -p fio smartmontools nvme-cli usbutils")
        return EXIT_REFUSED

    # --- resolve the device ----------------------------------------------
    try:
        dev = find_device(runner, config.device)
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
    logger.log(f"size   : {format_gib(dev.size_bytes)} ({dev.size_bytes} B)")
    logger.log(f"bus    : {dev.tran}")
    logger.log(f"smart  : smartctl {' '.join(mode) or '(auto)'}")
    logger.log(f"logs   : {log_dir}/")
    logger.log(f"mode   : {'READ + DESTRUCTIVE WRITE/VERIFY' if config.write else 'read-only'}")
    logger.log("")

    # --- safety + confirmation (write only) ------------------------------
    if config.write:
        rc = _guard_and_confirm(config, ctx, dev, logger)
        if rc != EXIT_OK:
            return rc

    # --- temperature source shared by thermal + fio monitor --------------
    temps: list[int] = []

    def read_temp() -> int | None:
        t = smart.read_temperature(runner, dev.path, mode)
        if t is not None:
            temps.append(t)
        return t

    thermal = ThermalController(
        config.policy, read_temp, sleep=ctx.sleep, log=lambda m: logger.log(f"   {m}")
    )
    fio_runner = FioRunner(
        read_temp=read_temp,
        policy=config.policy,
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
    verify = VerifyOutcome(VerifyStatus.SKIPPED)
    if config.write:
        rc = _recheck_before_write(ctx, dev, logger)
        if rc != EXIT_OK:
            return rc
        try:
            verify = _write_phase(config, ctx, dev, logger, thermal, fio_runner, log_dir)
        except (Exception, KeyboardInterrupt) as exc:
            # The device has been partially written; never exit with a bare
            # traceback and no verdict. fio itself is already killed by
            # run_region's finally - here we just report the unfinished state.
            # Log best-effort: if the failure *is* the log write, a raising
            # logger.log must not turn this into a bare-traceback exit 1.
            _safe_log(logger, "")
            _safe_log(logger, f"!! write phase failed: {exc!r}")
            _safe_log(logger, "RESULT: INCOMPLETE - the write was interrupted by an unexpected "
                      "error after the device was partially written. Cool it, replug, and resume "
                      "with --only <remaining parts>.")
            return EXIT_ATTENTION

    # --- post-write reporting: survival, benchmarks, SMART diff, summary --
    # Once the destructive write has run, an unexpected error below (e.g. a failed
    # log write, or fio/smartctl vanishing) must not escape as a bare traceback
    # and exit 1 - the contract reserves 1 for "refused, nothing written". Report
    # attention instead so the operator knows the device *was* written. The
    # write/verify line is logged inside this guard too, so a log failure there is
    # caught rather than escaping between the two guards.
    try:
        if config.write:
            logger.log(f"   write/verify: {verify.describe()}")
            logger.log("")
        return _report_and_finish(
            config, runner, dev, mode, logger, log_dir, before, verify, temps
        )
    except (Exception, KeyboardInterrupt) as exc:
        if not config.write:
            raise
        _safe_log(logger, "")
        _safe_log(logger, f"!! post-write reporting failed: {exc!r}")
        _safe_log(logger, "RESULT: INCOMPLETE - the device was written but the run could not "
                  "finish reporting. Inspect the logs above and re-check SMART manually.")
        return EXIT_ATTENTION


# --- helpers --------------------------------------------------------------


def _safe_log(logger: Logger, message: str) -> None:
    """Log for the post-write recovery paths without ever raising.

    Those handlers run after the device was written and must return
    EXIT_ATTENTION. If the very failure they are reporting is the log write (a
    full log disk), a plain ``logger.log`` would raise again and escape ``run``
    as exit 1 ("refused, nothing written") - the worst possible misreport. Fall
    back to stderr and, if even that fails, swallow it.
    """
    try:
        logger.log(message)
    except Exception:
        with contextlib.suppress(Exception):
            print(message, file=sys.stderr)


def _guard_and_confirm(config: RunConfig, ctx: RunContext, dev: Device, logger: Logger) -> int:
    """Run the pre-write guards, print failures, and take confirmation."""
    root = gather_root_info(ctx.runner)
    probe = gather_blank_probe(ctx.runner, dev, sys_block=ctx.sys_block)
    serials = all_serials(list_devices(ctx.runner))

    checks = evaluate_write_safety(
        dev, root=root, probe=probe, all_serials=serials, force=config.force
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
               f"{format_gib(dev.size_bytes)} | bus {dev.tran}")
    logger.log("")

    if not config.assume_yes:
        answer = ctx.confirm(f"Type the serial ({dev.serial}) to confirm: ")
        if answer.strip() != dev.serial or not dev.serial:
            logger.log("aborted (serial mismatch or empty).")
            return EXIT_REFUSED
    return EXIT_OK


def _recheck_before_write(ctx: RunContext, dev: Device, logger: Logger) -> int:
    """Last-line-of-defense re-check immediately before the write.

    Run after everything else (including the read-only SMART baseline) so the
    smallest possible window remains between it and ``fio``: the node must still
    name the same physical device (not reassigned by a replug) and be idle.
    """
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
    config: RunConfig, ctx: RunContext, dev: Device, logger: Logger,
    thermal: ThermalController, fio_runner: FioRunner, log_dir: Path,
) -> VerifyOutcome:
    """Run the quick or paced full write+verify."""
    if config.quick:
        # Clamp to the device so a small target verifies its whole span instead of
        # asking fio to write past the end (a spurious FAIL) - the quick default is
        # a fixed size chosen for large drives.
        region = quick_region(min(config.quick_bytes, dev.size_bytes))
        logger.log(f">> write+verify (crc32c, first {format_gib(region.size)} quick); "
                   f"ceiling {config.policy.ceiling_c} C")
        if thermal.prestart_ok():
            result = fio_runner.run_region(dev.path, region, log_dir / "fio_writeverify.log")
        else:
            result = RegionResult.OVERHEAT
        logger.log(f"   result: {result.value}")
        return VerifyOutcome(_REGION_TO_VERIFY[result])

    regions = plan_regions(dev.size_bytes, config.parts)
    selected = parse_only_spec(config.only, config.parts) if config.only else None
    sel_desc = f"parts {config.only}" if config.only else "all parts"
    logger.log(
        f">> write+verify (crc32c, full) in {config.parts} part(s), running {sel_desc}; "
        f"ceiling {config.policy.ceiling_c} C, cool to {config.policy.cool_target_c} C before each"
    )

    status = VerifyStatus.PASS
    ran = 0
    for region in regions:
        if selected is not None and region.index not in selected:
            logger.log(f">> part {region.index}/{config.parts}: skipped (not selected)")
            continue
        logger.log(f">> part {region.index}/{config.parts}  "
                   f"offset={format_gib(region.offset)}  size={format_gib(region.size)}")
        if not thermal.prestart_ok():
            status = VerifyStatus.OVERHEAT
            logger.log(f"   stopping before part {region.index} (too hot to start)")
            break
        result = fio_runner.run_region(
            dev.path, region, log_dir / f"fio_writeverify_part{region.index}.log"
        )
        ran += 1
        logger.log(f"   part {region.index}: {result.value}")
        if result is not RegionResult.PASS:
            status = _REGION_TO_VERIFY[result]
            logger.log(f"   stopping after part {region.index} ({result.value})")
            break

    if ran == 0:
        logger.log("   note: no parts ran")
    # Mark the outcome partial only when the selection is a *proper* subset - it
    # left some part unrun, so only those parts are verified, not the drive. An
    # --only that happens to cover every part (e.g. --parts 4 --only 1-4) verified
    # the whole drive in this run, so it is not partial.
    all_indices = {region.index for region in regions}
    is_proper_subset = selected is not None and selected != all_indices
    detail = (
        f"parts {config.only} of {config.parts}"
        if status is VerifyStatus.PASS and is_proper_subset
        else None
    )
    return VerifyOutcome(status, detail=detail)


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
    # Capture both streams so a diagnostic on stderr (e.g. on a failing read) is
    # never lost; stdout carries the report, stderr is usually empty.
    text_path.write_text(text.stdout + text.stderr)
    return smart.read_smart(runner, dev.path, mode)


def _smart_after(
    runner: Runner, dev: Device, mode: list[str], text_path: Path, dev_gone: bool
) -> SmartInfo:
    if dev_gone:
        text_path.write_text("device not present after run (disconnected) - SMART not read\n")
        return SmartInfo(raw=None)
    return _smart_snapshot(runner, dev, mode, text_path)


def _report_and_finish(
    config: RunConfig,
    runner: Runner,
    dev: Device,
    mode: list[str],
    logger: Logger,
    log_dir: Path,
    before: SmartInfo,
    verify: VerifyOutcome,
    temps: list[int],
) -> int:
    """Device-survival check, read benchmarks, post-run SMART diff and summary.

    Returns the process exit code. Split out from :func:`run` so the whole
    post-write tail can be wrapped in one error guard (see the call site).
    """
    # --- did the device survive? -----------------------------------------
    dev_gone = config.write and not _device_present(runner, dev)
    if dev_gone:
        logger.log(f"!! {dev.path} is gone or changed identity since the write started.")
        logger.log("   It dropped off the bus (commonly a thermal disconnect on a passive")
        logger.log("   USB enclosure). Skipping read benchmarks and post-SMART. Let it cool,")
        logger.log("   replug, and resume with --only <remaining parts>.")
        logger.log("")

    # --- read benchmarks --------------------------------------------------
    # The read benchmarks run unpaced (~90s of sustained IO with no cooldown or
    # ceiling monitor - that machinery is the write path's alone). Skip them when
    # the write phase already flagged attention: after an OVERHEAT stop the drive
    # is sitting near the ceiling, and hammering it now could provoke the very
    # bridge disconnect the pacing exists to avoid; after a verify FAIL the run is
    # already flagged, so there's nothing to gain by stressing it further. A
    # read-only run has status SKIPPED (not needs_attention), so its benchmarks -
    # the whole point of that mode - still run.
    read_error = False
    if dev_gone:
        pass  # device already gone; survival note logged above
    elif verify.needs_attention:
        logger.log(">> read benchmarks: skipped (write phase needs attention - "
                   "not stressing the drive further)")
        logger.log("")
    else:
        read_error = _read_benchmarks(runner, dev, logger, log_dir)

    # --- SMART after + diff ----------------------------------------------
    after = _smart_after(runner, dev, mode, log_dir / "smart_after.txt", dev_gone)
    deltas = diff_smart(before, after)
    regressions = health_regressions(before, after)
    verdict = classify_smart(after, deltas, regressions)

    # --- summary ----------------------------------------------------------
    peak = max(temps) if temps else None
    logger.log("== summary ==")
    logger.log(f"write/verify : {verify.describe()}")
    logger.log(f"peak temp    : {peak} C")
    logger.log(f"SMART diff   : {describe_verdict(verdict)}")
    for d in deltas:
        logger.log(f"   {d.field}: {d.before} -> {d.after}")
    for r in regressions:
        logger.log(f"   {r}")
    logger.log(f"full logs    : {log_dir}/")
    logger.log("")

    if dev_gone or verify.needs_attention or verdict is not SmartVerdict.CLEAN or read_error:
        if dev_gone:
            logger.log("RESULT: INCOMPLETE - device disconnected mid-run (likely thermal). "
                       "Cool it, replug, and resume with --only.")
        elif verify.status is VerifyStatus.OVERHEAT:
            logger.log("RESULT: INCOMPLETE - stopped on temperature ceiling; "
                       "use more --parts (and/or a fan).")
        elif read_error:
            logger.log("RESULT: ATTENTION NEEDED - a read benchmark hit an IO error "
                       "(possible unreadable sectors); inspect the logs above.")
        else:
            logger.log("RESULT: ATTENTION NEEDED - inspect the logs above.")
        return EXIT_ATTENTION

    logger.log("RESULT: OK")
    return EXIT_OK


def _read_benchmarks(runner: Runner, dev: Device, logger: Logger, log_dir: Path) -> bool:
    """Run the read benchmarks. Returns True if any job reported a read IO error
    (e.g. unreadable sectors), so the caller can raise the overall verdict to
    "needs attention" rather than let a bad read pass silently.
    """
    read_error = False
    for kind in ReadKind:
        logger.log(f">> {kind.label}")
        result = runner.run(build_read_argv(dev.path, kind))
        (log_dir / f"fio_{kind.value}.json").write_text(result.stdout)
        try:
            stats = parse_read_json(result.json(), kind)
        except FioReadError as exc:
            # A real read failure, not unparseable output: surface it and flag
            # the run, don't bury it as "could not parse".
            logger.log(f"   !! read error: {exc} - possible unreadable sectors")
            read_error = True
            continue
        except ValueError:
            # No parseable JSON. If fio also exited non-zero, the read itself
            # failed (e.g. it bailed on an unreadable sector before emitting a
            # report) - flag attention rather than bury it as a parse hiccup.
            if not result.ok:
                logger.log(f"   !! read error: fio exited {result.returncode} with no "
                           "parseable output - possible unreadable sectors")
                read_error = True
            else:
                logger.log("   (could not parse fio output)")
            continue
        if kind is ReadKind.SEQ:
            logger.log(f"   bandwidth: {stats.bw_mb:.0f} MB/s")
        else:
            logger.log(f"   IOPS: {stats.iops:.0f} ({stats.bw_mb:.0f} MB/s)")
    logger.log("")
    return read_error


def _health_str(info: SmartInfo) -> str:
    if not info.has_report:
        return "?"
    passed = {True: "PASSED", False: "FAILED", None: "?"}[info.health_passed]
    return f"{passed} (temp {info.temperature_c} C, media_errors {info.media_errors})"
