"""Integration tests for the end-to-end run, driven by a fake command runner.

These exercise the wiring and exit codes for the read-only happy path and the
safety-refusal path without touching hardware or running real commands.
"""

from __future__ import annotations

import io
import json
import shutil

import pytest

from drivetest.config import (
    DEFAULT_PARTS,
    DEFAULT_QUICK_BYTES,
    DEFAULT_THERMAL_POLICY,
    RunConfig,
)
from drivetest.fio import RegionResult
from drivetest.orchestrator import (
    _REGION_TO_VERIFY,
    EXIT_ATTENTION,
    EXIT_OK,
    EXIT_REFUSED,
    RunContext,
    run,
)
from drivetest.proc import Result
from drivetest.units import GIB

from .conftest import Call, FakeRunner, load_text


def test_region_to_verify_mapping_is_exhaustive():
    # Every fio region result must map to a verify status, or a write phase
    # would KeyError at runtime.
    assert set(_REGION_TO_VERIFY) == set(RegionResult)


@pytest.fixture(autouse=True)
def _no_missing_tools(monkeypatch):  # pyright: ignore[reportUnusedFunction]  # autouse: pytest calls it
    # Decouple tests from what happens to be installed on the host.
    monkeypatch.setattr("drivetest.orchestrator.missing_tools", lambda required: [])


def _config(
    device, *, write=False, assume_yes=False, quick=False, force=False, parts=DEFAULT_PARTS,
    only=None,
) -> RunConfig:
    """A RunConfig for these tests, defaulting the knobs a run doesn't vary."""
    return RunConfig(
        device=device,
        write=write,
        quick=quick,
        force=force,
        only=only,
        assume_yes=assume_yes,
        log_dir=None,
        parts=parts,
        quick_bytes=DEFAULT_QUICK_BYTES,
        policy=DEFAULT_THERMAL_POLICY,
    )


def _ctx(runner, tmp_path, **kw) -> RunContext:
    return RunContext(
        runner=runner,
        workdir=tmp_path,
        stamp="TEST",
        sleep=lambda _s: None,
        confirm=kw.pop("confirm", lambda _p: ""),
        **kw,
    )


def test_readonly_happy_path(tmp_path):
    runner = FakeRunner()
    runner.add("lsblk", contains=["-Jb"], stdout=load_text("lsblk_usb_sda.json"))
    runner.add("smartctl", contains=["-i"], returncode=0)
    runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    runner.add("smartctl", contains=["-x"], stdout="Serial Number: 255106803016")
    runner.add("fio", contains=["seqread"], stdout=load_text("fio_seqread.json"))
    runner.add("fio", contains=["randread"], stdout=load_text("fio_randread.json"))

    code = run(_config("/dev/sda"), _ctx(runner, tmp_path))
    assert code == EXIT_OK

    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "RESULT: OK" in summary
    assert "read-only" in summary
    assert "1002 MB/s" in summary  # parsed from fio JSON, not grepped


def test_write_refused_on_system_disk(tmp_path):
    runner = FakeRunner()
    runner.add("lsblk", contains=["-nrso"], stdout="nvme0n1p4\nnvme0n1\n")  # root walk
    runner.add("lsblk", contains=["-Jb"], stdout=load_text("lsblk_nvme_system.json"))
    runner.add("smartctl", contains=["-i"], returncode=0)
    runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    runner.add("smartctl", contains=["-x"], stdout="Serial Number: x")
    runner.add(
        "findmnt",
        stdout='{"filesystems": [{"source": "/dev/nvme0n1p4", "target": "/"}]}',
    )
    runner.add("wipefs", stdout='{"signatures": []}')

    confirmed = []
    ctx = _ctx(runner, tmp_path, confirm=lambda p: confirmed.append(p) or "")
    code = run(_config("/dev/nvme0n1", write=True, assume_yes=False), ctx)

    assert code == EXIT_REFUSED
    summary = (tmp_path / "drive_test_S3ZHNF0KC28756_TEST" / "summary.log").read_text()
    assert "refusing to write" in summary
    assert "not-system-disk" in summary
    # never reached the destructive confirmation
    assert confirmed == []


def test_device_not_found(tmp_path):
    runner = FakeRunner()
    runner.add("lsblk", contains=["-Jb"], stdout='{"blockdevices": []}')
    code = run(_config("/dev/nope"), _ctx(runner, tmp_path))
    assert code == EXIT_REFUSED


def test_run_refuses_when_required_tools_missing(tmp_path, monkeypatch):
    # Missing a required binary refuses the run up front (EXIT_REFUSED), before the
    # device is even resolved. Overrides the autouse "no missing tools" fixture.
    monkeypatch.setattr("drivetest.orchestrator.missing_tools", lambda required: ["fio"])
    code = run(_config("/dev/sda"), _ctx(FakeRunner(), tmp_path))
    assert code == EXIT_REFUSED


class _SequencedSmartRunner(FakeRunner):
    """A FakeRunner whose ``smartctl --json`` calls return a scripted sequence,
    so the before/after health snapshots can differ.
    """

    def __init__(self, json_sequence):
        super().__init__()
        self._seq = iter(json_sequence)

    def run(self, argv, *, input=None, timeout=None):
        argv_list = list(argv)
        # Only the full-report (`-x`) snapshots are scripted; the `--json -i`
        # access-mode probe falls through to the registered rules.
        if argv_list[0] == "smartctl" and "--json" in argv_list and "-x" in argv_list:
            self.record(argv, input=input, timeout=timeout)
            return Result(tuple(argv_list), 0, next(self._seq), "")
        return super().run(argv, input=input, timeout=timeout)


def test_readonly_flags_worsened_smart(tmp_path):
    # after-snapshot shows new media errors -> ATTENTION even without a write.
    good = load_text("smart_nvme.json")
    bad = json.dumps(
        {**json.loads(good),
         "nvme_smart_health_information_log": {
             **json.loads(good)["nvme_smart_health_information_log"], "media_errors": 5}}
    )
    # --json calls in read-only order: baseline temp, before-snapshot, after-snapshot.
    runner = _SequencedSmartRunner([good, good, bad])
    runner.add("lsblk", contains=["-Jb"], stdout=load_text("lsblk_usb_sda.json"))
    runner.add("smartctl", contains=["-i"], returncode=0)
    runner.add("smartctl", contains=["-x"], stdout="Serial Number: x")
    runner.add("fio", contains=["seqread"], stdout=load_text("fio_seqread.json"))
    runner.add("fio", contains=["randread"], stdout=load_text("fio_randread.json"))

    code = run(_config("/dev/sda"), _ctx(runner, tmp_path))
    assert code == EXIT_ATTENTION


def test_readonly_read_benchmark_io_error_is_attention(tmp_path):
    # A read benchmark whose fio job reports an IO error (unreadable sectors) must
    # raise the run to ATTENTION and be surfaced, not buried as "could not parse".
    runner = FakeRunner()
    runner.add("lsblk", contains=["-Jb"], stdout=load_text("lsblk_usb_sda.json"))
    runner.add("smartctl", contains=["-i"], returncode=0)
    runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    runner.add("smartctl", contains=["-x"], stdout="Serial Number: 255106803016")
    runner.add("fio", contains=["seqread"], stdout='{"jobs": [{"error": 5, "read": {}}]}')
    runner.add("fio", contains=["randread"], stdout=load_text("fio_randread.json"))

    code = run(_config("/dev/sda"), _ctx(runner, tmp_path))
    assert code == EXIT_ATTENTION
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "read error" in summary
    assert "unreadable sectors" in summary


def test_readonly_read_benchmark_hard_failure_is_attention(tmp_path):
    # fio exits non-zero and emits no parseable JSON (it bailed on an unreadable
    # sector before printing a report). This must still be flagged ATTENTION, not
    # silently downgraded to a "could not parse" note that would exit OK.
    runner = FakeRunner()
    runner.add("lsblk", contains=["-Jb"], stdout=load_text("lsblk_usb_sda.json"))
    runner.add("smartctl", contains=["-i"], returncode=0)
    runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    runner.add("smartctl", contains=["-x"], stdout="Serial Number: 255106803016")
    runner.add("fio", contains=["seqread"], stdout="fio: I/O error", returncode=1)
    runner.add("fio", contains=["randread"], stdout=load_text("fio_randread.json"))

    code = run(_config("/dev/sda"), _ctx(runner, tmp_path))
    assert code == EXIT_ATTENTION
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "read error" in summary
    assert "unreadable sectors" in summary


def test_readonly_unparseable_read_with_zero_exit_is_not_attention(tmp_path):
    # The counterpart: garbage output but fio exited 0 (a benign parse hiccup, not a
    # read failure) stays a note and does not by itself raise the run to ATTENTION.
    runner = FakeRunner()
    runner.add("lsblk", contains=["-Jb"], stdout=load_text("lsblk_usb_sda.json"))
    runner.add("smartctl", contains=["-i"], returncode=0)
    runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    runner.add("smartctl", contains=["-x"], stdout="Serial Number: 255106803016")
    runner.add("fio", contains=["seqread"], stdout="not json", returncode=0)
    runner.add("fio", contains=["randread"], stdout="not json", returncode=0)

    code = run(_config("/dev/sda"), _ctx(runner, tmp_path))
    assert code == EXIT_OK
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "could not parse fio output" in summary


# --- destructive write paths ----------------------------------------------


class _DoneProc:
    """A fake fio process that has already finished with a given return code.

    ``poll`` returns the code (not None), so the run_region monitor sees it as
    finished immediately and no real subprocess is needed.
    """

    def __init__(self, returncode: int = 0) -> None:
        self._rc = returncode
        self.stdout = io.StringIO("")  # empty: the drain thread does nothing

    def poll(self) -> int:
        return self._rc

    def wait(self, timeout: float | None = None) -> int:
        return self._rc

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


def _sys_block_for(tmp_path, name: str = "sda") -> str:
    """A realistic /sys/block root with the device's (empty) holders dir, so the
    blank probe sees a real whole disk rather than failing closed.
    """
    sysroot = tmp_path / "sysblock"
    (sysroot / name / "holders").mkdir(parents=True)
    return str(sysroot)


def _write_runner() -> FakeRunner:
    """A runner whose guards all pass for a blank, non-system USB disk (sda)."""
    runner = FakeRunner()
    runner.add("lsblk", contains=["-nrso"], stdout="nvme0n1p4\nnvme0n1\n")  # root walk
    runner.add("lsblk", contains=["-Jb"], stdout=load_text("lsblk_usb_sda.json"))
    runner.add("findmnt", stdout='{"filesystems": [{"source": "/dev/nvme0n1p4", "target": "/"}]}')
    runner.add("wipefs", stdout='{"signatures": []}')
    runner.add("smartctl", contains=["-i"], returncode=0)
    runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    runner.add("smartctl", contains=["-x"], stdout="Serial Number: 255106803016")
    runner.add("fio", contains=["seqread"], stdout=load_text("fio_seqread.json"))
    runner.add("fio", contains=["randread"], stdout=load_text("fio_randread.json"))
    return runner


def test_write_happy_path_passes(tmp_path):
    runner = _write_runner()
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda _argv: _DoneProc(0),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True), ctx)
    assert code == EXIT_OK

    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "RESULT: OK" in summary
    assert "DESTRUCTIVE" in summary
    assert "write/verify : PASS" in summary


def test_write_refused_on_serial_mismatch_confirmation(tmp_path):
    # All guards pass, but the interactive confirmation gets the wrong serial: the
    # write is refused and fio is never launched. Exercises the human-confirm path
    # (the other write tests use assume_yes=True and skip it).
    runner = _write_runner()
    popen_calls = []
    ctx = _ctx(
        runner, tmp_path,
        confirm=lambda _p: "WRONG-SERIAL",
        popen=lambda argv: popen_calls.append(argv) or _DoneProc(0),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=False), ctx)
    assert code == EXIT_REFUSED
    assert popen_calls == []  # never started a write
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "aborted (serial mismatch" in summary


def test_write_aborts_when_device_vanishes_before_write(tmp_path):
    # The node disappears between confirmation and the pre-write re-check: abort
    # with "device vanished", refusing the write; fio must never launch.
    original = load_text("lsblk_usb_sda.json")
    gone = '{"blockdevices": []}'
    # -Jb calls: find_device, list_devices (serials), then the re-check (gone).
    runner = _LsblkSequenceRunner([original, original, gone])
    runner.add("lsblk", contains=["-nrso"], stdout="nvme0n1p4\nnvme0n1\n")
    runner.add("findmnt", stdout='{"filesystems": [{"source": "/dev/nvme0n1p4", "target": "/"}]}')
    runner.add("wipefs", stdout='{"signatures": []}')
    runner.add("smartctl", contains=["-i"], returncode=0)
    runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    runner.add("smartctl", contains=["-x"], stdout="Serial Number: 255106803016")
    popen_calls = []
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda argv: popen_calls.append(argv) or _DoneProc(0),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True), ctx)
    assert code == EXIT_REFUSED
    assert popen_calls == []  # never started a write
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "device vanished after confirmation" in summary


def test_write_fio_verify_failure_is_attention(tmp_path):
    # fio exits non-zero (a verify mismatch) -> FAIL -> ATTENTION.
    runner = _write_runner()
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda _argv: _DoneProc(1),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True), ctx)
    assert code == EXIT_ATTENTION
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "FAIL" in summary


def test_write_phase_error_reports_incomplete(tmp_path):
    # An unexpected error after the device is partially written must yield a
    # clear INCOMPLETE verdict, never a bare traceback (the device was touched).
    def boom_popen(_argv):
        raise OSError("failed to spawn fio")

    runner = _write_runner()
    ctx = _ctx(
        runner, tmp_path,
        popen=boom_popen,
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True), ctx)
    assert code == EXIT_ATTENTION
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "RESULT: INCOMPLETE" in summary
    assert "write phase failed" in summary


def test_post_write_reporting_error_reports_incomplete_not_refused(tmp_path, monkeypatch):
    # An unexpected error AFTER the destructive write (here: reading benchmarks)
    # must not escape as exit 1 - which the contract reserves for "refused,
    # nothing written" - but become an INCOMPLETE attention exit.
    def boom(*_args, **_kwargs):
        raise OSError("log volume disappeared")

    monkeypatch.setattr("drivetest.orchestrator._read_benchmarks", boom)
    runner = _write_runner()
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda _argv: _DoneProc(0),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True), ctx)
    assert code == EXIT_ATTENTION  # not EXIT_REFUSED
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "post-write reporting failed" in summary
    assert "device was written" in summary


def test_safe_log_never_raises_and_falls_back_to_stderr(capsys):
    # The recovery helper must swallow a raising logger and fall back to stderr, so
    # a failing log write can't turn a post-write recovery into an exit-1 traceback.
    from drivetest.orchestrator import _safe_log

    class _BoomLogger:
        def log(self, message: str) -> None:
            raise OSError("log disk full")

    _safe_log(_BoomLogger(), "recovery note")  # type: ignore[arg-type]  # test double
    assert "recovery note" in capsys.readouterr().err


def test_post_write_recovery_survives_a_failing_logger(tmp_path, monkeypatch, capsys):
    # The worst case the recovery guard exists for: the post-write failure *is* the
    # log write (the log volume vanished). The recovery must still return ATTENTION
    # - never let a raising logger.log escape run() as exit 1 - and fall back to
    # stderr rather than crash.
    log_root = tmp_path / "drive_test_TAD0NT005915_TEST"

    def boom(_runner, _dev, _logger, _log_dir):
        shutil.rmtree(log_root)  # the log directory disappears mid-run...
        raise OSError("log volume disappeared")  # ...and then reporting fails

    monkeypatch.setattr("drivetest.orchestrator._read_benchmarks", boom)
    runner = _write_runner()
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda _argv: _DoneProc(0),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True), ctx)
    assert code == EXIT_ATTENTION  # not EXIT_REFUSED, despite the logger failing too
    # the recovery message could not be written to the (gone) log, so it went to stderr
    assert "post-write reporting failed" in capsys.readouterr().err


class _LsblkSequenceRunner(FakeRunner):
    """Serves a scripted sequence of ``lsblk -Jb`` outputs (last repeats), so the
    pre-write identity re-check can observe a different device than confirmation.
    """

    def __init__(self, jb_sequence):
        super().__init__()
        self._jb = list(jb_sequence)
        self._i = 0

    def run(self, argv, *, input=None, timeout=None):
        argv_list = list(argv)
        if argv_list[0] == "lsblk" and "-Jb" in argv_list:
            self.record(argv, input=input, timeout=timeout)
            out = self._jb[min(self._i, len(self._jb) - 1)]
            self._i += 1
            return Result(tuple(argv_list), 0, out, "")
        return super().run(argv, input=input, timeout=timeout)


def test_write_aborts_when_identity_changes_before_write(tmp_path):
    # The node is reassigned to a different disk between confirmation and the
    # pre-write re-check: the write must abort, and fio must never be launched.
    original = load_text("lsblk_usb_sda.json")
    swapped = json.dumps(
        {"blockdevices": [{**json.loads(original)["blockdevices"][0], "serial": "DIFFERENT"}]}
    )
    # -Jb calls: find_device (top), list_devices (serials), then the re-check.
    runner = _LsblkSequenceRunner([original, original, swapped])
    runner.add("lsblk", contains=["-nrso"], stdout="nvme0n1p4\nnvme0n1\n")
    runner.add("findmnt", stdout='{"filesystems": [{"source": "/dev/nvme0n1p4", "target": "/"}]}')
    runner.add("wipefs", stdout='{"signatures": []}')
    runner.add("smartctl", contains=["-i"], returncode=0)
    runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    runner.add("smartctl", contains=["-x"], stdout="Serial Number: x")

    popen_calls = []
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda argv: popen_calls.append(argv) or _DoneProc(0),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True), ctx)

    assert code == EXIT_REFUSED
    assert popen_calls == []  # never started a write
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "identity changed" in summary


def test_write_aborts_on_thermal_ceiling(tmp_path, monkeypatch):
    # Too hot to start: the write phase must stop with OVERHEAT, never launch fio,
    # and the run must exit ATTENTION with the ceiling explanation.
    monkeypatch.setattr(
        "drivetest.orchestrator.ThermalController.prestart_ok", lambda self: False
    )
    runner = _write_runner()
    popen_calls = []
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda argv: popen_calls.append(argv) or _DoneProc(0),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True), ctx)
    assert code == EXIT_ATTENTION
    assert popen_calls == []  # too hot -> fio never launched
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "temperature ceiling" in summary
    assert "too hot to start" in summary


def _fio_bench_calls(runner: FakeRunner) -> list[Call]:
    """The read-benchmark fio invocations recorded by the runner.

    The write+verify pass runs through ``ctx.popen`` (the FioRunner), so the only
    ``fio`` commands that reach ``runner.run`` are the post-write read benchmarks.
    """
    return [c for c in runner.calls if c.argv and c.argv[0] == "fio"]


def test_write_overheat_skips_read_benchmarks(tmp_path, monkeypatch):
    # After an OVERHEAT stop the drive is near the ceiling; the unpaced read
    # benchmarks must be skipped rather than provoke the disconnect the pacing
    # exists to avoid. The run still exits ATTENTION with the ceiling explanation.
    monkeypatch.setattr(
        "drivetest.orchestrator.ThermalController.prestart_ok", lambda self: False
    )
    runner = _write_runner()
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda _argv: _DoneProc(0),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True), ctx)
    assert code == EXIT_ATTENTION
    assert _fio_bench_calls(runner) == []  # benchmarks never ran
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "read benchmarks: skipped" in summary
    assert "temperature ceiling" in summary


def test_write_verify_failure_skips_read_benchmarks(tmp_path):
    # A verify FAIL already flags the run; there's no value in stressing the drive
    # with the read benchmarks afterward, so they are skipped too.
    runner = _write_runner()
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda _argv: _DoneProc(1),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True), ctx)
    assert code == EXIT_ATTENTION
    assert _fio_bench_calls(runner) == []  # benchmarks never ran
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "read benchmarks: skipped" in summary


def test_write_pass_still_runs_read_benchmarks(tmp_path):
    # A clean write PASS is not "needs attention", so the read benchmarks run as
    # usual - the skip must not swallow the normal post-write benchmarks.
    runner = _write_runner()
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda _argv: _DoneProc(0),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True), ctx)
    assert code == EXIT_OK
    assert len(_fio_bench_calls(runner)) == 2  # seqread + randread both ran
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "read benchmarks: skipped" not in summary


def test_write_stops_after_a_failed_part_and_does_not_continue(tmp_path):
    # A FAIL on part 2 of 3 must stop the run: part 3 never starts (no plowing on
    # over a drive that just failed verify).
    runner = _write_runner()
    launched = []

    def popen(argv):
        launched.append(argv)
        # The second launched part fails its verify (fio exits non-zero).
        return _DoneProc(1 if len(launched) == 2 else 0)

    ctx = _ctx(runner, tmp_path, popen=popen, sys_block=_sys_block_for(tmp_path))
    code = run(_config("/dev/sda", write=True, assume_yes=True, parts=3), ctx)
    assert code == EXIT_ATTENTION
    assert len(launched) == 2  # parts 1-2 ran; part 3 never launched
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "stopping after part 2" in summary
    assert "part 3/3" not in summary  # part 3 was never even announced


def test_write_only_subset_runs_selected_parts_and_flags_partial(tmp_path):
    # --only 2-3 of 4: only those parts run, and a clean pass is flagged partial
    # ("not the whole drive") while still exiting OK.
    runner = _write_runner()
    launched = []
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda argv: launched.append(argv) or _DoneProc(0),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True, parts=4, only="2-3"), ctx)
    assert code == EXIT_OK
    assert len(launched) == 2  # parts 2 and 3 only
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "not the whole drive" in summary
    assert "skipped (not selected)" in summary


def test_write_only_covering_all_parts_is_not_flagged_partial(tmp_path):
    # --only 1-4 of 4 covers every part, so this run verified the whole drive: it
    # must NOT be labelled "not the whole drive" even though --only was passed.
    runner = _write_runner()
    launched = []
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda argv: launched.append(argv) or _DoneProc(0),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True, parts=4, only="1-4"), ctx)
    assert code == EXIT_OK
    # 5 fio passes: one each for parts 1-3, plus part 4 as a body + sub-MiB tail
    # pass (the device size is not a whole multiple of parts x 1 MiB).
    assert len(launched) == 5
    assert "--bs=90112" in launched[-1]  # the trailing short block covers the tail
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "not the whole drive" not in summary
    assert "write/verify : PASS" in summary


def test_write_quick_mode_runs_a_single_region(tmp_path):
    # --quick writes one leading region, not the whole drive in parts.
    runner = _write_runner()
    launched = []
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda argv: launched.append(argv) or _DoneProc(0),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True, quick=True), ctx)
    assert code == EXIT_OK
    assert len(launched) == 1
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "quick" in summary
    assert "write/verify : PASS" in summary


def test_write_quick_clamps_region_to_a_small_device(tmp_path):
    # The quick span is a fixed size chosen for large drives; on a device smaller
    # than that it must clamp to the device size, not ask fio to write past the end.
    small_size = 10 * GIB
    small = json.loads(load_text("lsblk_usb_sda.json"))
    small["blockdevices"][0]["size"] = small_size
    runner = FakeRunner()
    runner.add("lsblk", contains=["-nrso"], stdout="nvme0n1p4\nnvme0n1\n")
    runner.add("lsblk", contains=["-Jb"], stdout=json.dumps(small))
    runner.add("findmnt", stdout='{"filesystems": [{"source": "/dev/nvme0n1p4", "target": "/"}]}')
    runner.add("wipefs", stdout='{"signatures": []}')
    runner.add("smartctl", contains=["-i"], returncode=0)
    runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    runner.add("smartctl", contains=["-x"], stdout="Serial Number: 255106803016")
    runner.add("fio", contains=["seqread"], stdout=load_text("fio_seqread.json"))
    runner.add("fio", contains=["randread"], stdout=load_text("fio_randread.json"))

    launched: list[list[str]] = []
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda argv: launched.append(argv) or _DoneProc(0),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True, quick=True), ctx)
    assert code == EXIT_OK
    [argv] = launched
    # the region is clamped to the whole small device, not the 50 GiB quick default
    assert f"--size={small_size}" in argv


def test_write_aborts_when_device_mounted_at_recheck(tmp_path):
    # Same device (identity stable) but mounted by the pre-write re-check: abort on
    # the mount guard, and fio must never launch.
    original = load_text("lsblk_usb_sda.json")
    mounted = json.dumps(
        {"blockdevices": [
            {**json.loads(original)["blockdevices"][0], "mountpoints": ["/mnt/data"]}
        ]}
    )
    # -Jb calls: find_device, list_devices, then the mounted re-check.
    runner = _LsblkSequenceRunner([original, original, mounted])
    runner.add("lsblk", contains=["-nrso"], stdout="nvme0n1p4\nnvme0n1\n")
    runner.add("findmnt", stdout='{"filesystems": [{"source": "/dev/nvme0n1p4", "target": "/"}]}')
    runner.add("wipefs", stdout='{"signatures": []}')
    runner.add("smartctl", contains=["-i"], returncode=0)
    runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    runner.add("smartctl", contains=["-x"], stdout="Serial Number: 255106803016")
    popen_calls = []
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda argv: popen_calls.append(argv) or _DoneProc(0),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True), ctx)
    assert code == EXIT_REFUSED
    assert popen_calls == []  # never started a write
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "is mounted at" in summary


def test_write_reports_disconnect_when_device_vanishes(tmp_path):
    # The device drops off the bus after the write (a thermal disconnect on a
    # passive enclosure): read benchmarks and post-SMART are skipped and the run
    # ends INCOMPLETE, not OK.
    original = load_text("lsblk_usb_sda.json")
    gone = '{"blockdevices": []}'
    # -Jb calls: find_device, list_devices, pre-write re-check, post-write presence.
    runner = _LsblkSequenceRunner([original, original, original, gone])
    runner.add("lsblk", contains=["-nrso"], stdout="nvme0n1p4\nnvme0n1\n")
    runner.add("findmnt", stdout='{"filesystems": [{"source": "/dev/nvme0n1p4", "target": "/"}]}')
    runner.add("wipefs", stdout='{"signatures": []}')
    runner.add("smartctl", contains=["-i"], returncode=0)
    runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    runner.add("smartctl", contains=["-x"], stdout="Serial Number: 255106803016")
    ctx = _ctx(
        runner, tmp_path,
        popen=lambda _argv: _DoneProc(0),
        sys_block=_sys_block_for(tmp_path),
    )
    code = run(_config("/dev/sda", write=True, assume_yes=True), ctx)
    assert code == EXIT_ATTENTION
    summary = (tmp_path / "drive_test_TAD0NT005915_TEST" / "summary.log").read_text()
    assert "disconnected mid-run" in summary
    assert "is gone or changed identity" in summary
