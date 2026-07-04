"""Tests for fio argv building, JSON parsing, region classification and the
temperature monitor. A real-fio integration test covers the happy write path.
"""

from __future__ import annotations

import io
import shutil
import subprocess

import pytest

from drivetest.config import DEFAULT_THERMAL_POLICY
from drivetest.fio import (
    FioReadError,
    FioRunner,
    ReadKind,
    RegionResult,
    build_read_argv,
    build_writeverify_argv,
    classify_region,
    default_popen,
    monitor_region,
    parse_read_json,
)
from drivetest.planning import Region
from drivetest.units import KIB, MIB

from .conftest import collect_sleep, load_json

POLICY = DEFAULT_THERMAL_POLICY


# --- argv building --------------------------------------------------------

def test_writeverify_argv_has_verify_and_region():
    argv = build_writeverify_argv("/dev/sdx", Region(3, 1024, 2048))
    assert "--verify=crc32c" in argv
    assert "--do_verify=1" in argv
    assert "--verify_fatal=1" in argv
    assert "--offset=1024" in argv
    assert "--size=2048" in argv
    assert "--filename=/dev/sdx" in argv


def test_read_argv_shapes():
    seq = build_read_argv("/dev/sdx", ReadKind.SEQ)
    assert "--bs=1M" in seq and "--rw=read" in seq and "--output-format=json" in seq
    # cache-bypass and time-bounded whole-device pass are essential to the benchmark
    assert "--direct=1" in seq and "--time_based" in seq and "--size=100%" in seq
    assert "--name=seqread" in seq
    rnd = build_read_argv("/dev/sdx", ReadKind.RAND)
    assert "--bs=4k" in rnd and "--rw=randread" in rnd


def test_read_kind_label():
    assert ReadKind.SEQ.label == "sequential read (1M, qd32, 60s)"
    assert ReadKind.RAND.label == "random read (4k, qd64, 30s)"


# --- JSON parsing ---------------------------------------------------------

def test_parse_seqread_json():
    stats = parse_read_json(load_json("fio_seqread.json"), ReadKind.SEQ)
    assert stats.kind is ReadKind.SEQ
    assert stats.bw_bytes == 1002438656
    assert round(stats.bw_mb) == 1002
    assert round(stats.iops) == 956


def test_parse_randread_json():
    stats = parse_read_json(load_json("fio_randread.json"), ReadKind.RAND)
    assert round(stats.iops) == 69905


def test_parse_read_json_falls_back_to_kib_bw():
    # older fio without bw_bytes
    obj = {"jobs": [{"read": {"bw": 1000, "iops": 10}}]}
    stats = parse_read_json(obj, ReadKind.SEQ)
    assert stats.bw_bytes == 1000 * KIB


def test_parse_read_json_no_jobs():
    with pytest.raises(ValueError):
        parse_read_json({"jobs": []}, ReadKind.SEQ)


def test_parse_read_json_raises_on_job_error():
    # A non-zero fio job error means the run failed; its numbers are unreliable.
    with pytest.raises(ValueError):
        parse_read_json({"jobs": [{"error": 5, "read": {"bw_bytes": 1, "iops": 1}}]}, ReadKind.SEQ)


def test_parse_read_json_job_error_is_a_distinct_fioreaderror():
    # A real read failure raises FioReadError (a ValueError subclass), so the
    # caller can tell it apart from merely-unparseable output and surface it.
    with pytest.raises(FioReadError):
        parse_read_json({"jobs": [{"error": 5, "read": {}}]}, ReadKind.SEQ)
    assert issubclass(FioReadError, ValueError)
    # An unparseable/missing-figure result stays a plain ValueError, not FioReadError.
    with pytest.raises(ValueError) as exc:
        parse_read_json({"jobs": [{"read": {"iops": 10}}]}, ReadKind.SEQ)
    assert not isinstance(exc.value, FioReadError)


def test_parse_read_json_raises_on_missing_bandwidth():
    # Missing bandwidth must not be reported as a genuine 0 B/s.
    with pytest.raises(ValueError):
        parse_read_json({"jobs": [{"read": {"iops": 10}}]}, ReadKind.SEQ)


def test_parse_read_json_raises_on_missing_iops():
    with pytest.raises(ValueError):
        parse_read_json({"jobs": [{"read": {"bw_bytes": 1000}}]}, ReadKind.SEQ)


# --- region classification ------------------------------------------------

def test_classify_region():
    assert classify_region(False, 0) is RegionResult.PASS
    assert classify_region(False, 1) is RegionResult.FAIL
    assert classify_region(True, 0) is RegionResult.OVERHEAT
    assert classify_region(True, 137) is RegionResult.OVERHEAT  # overheat wins


# --- temperature monitor --------------------------------------------------

def test_monitor_kills_on_ceiling_breach():
    # third sample reaches the ceiling and must trigger a kill
    temps = iter([POLICY.ceiling_c - 20, POLICY.ceiling_c - 10, POLICY.ceiling_c])
    killed = []
    sleep, _ = collect_sleep()
    overheat = monitor_region(
        is_alive=lambda: True,
        read_temp=lambda: next(temps),
        policy=POLICY,
        sleep=sleep,
        kill=lambda: killed.append(True),
        on_sample=lambda _t: None,
    )
    assert overheat
    assert killed == [True]


def test_monitor_returns_false_when_process_finishes_cool():
    alive = iter([True, True, False])
    temps = iter([40, 45])
    killed = []
    sleep, _ = collect_sleep()
    overheat = monitor_region(
        is_alive=lambda: next(alive),
        read_temp=lambda: next(temps),
        policy=POLICY,
        sleep=sleep,
        kill=lambda: killed.append(True),
        on_sample=lambda _t: None,
    )
    assert not overheat
    assert killed == []


def test_monitor_ignores_unreadable_temps():
    alive = iter([True, True, False])
    temps = iter([None, None])
    sleep, _ = collect_sleep()
    overheat = monitor_region(
        is_alive=lambda: next(alive),
        read_temp=lambda: next(temps),
        policy=POLICY,
        sleep=sleep,
        kill=lambda: pytest.fail("should not kill on unreadable temp"),
        on_sample=lambda _t: None,
    )
    assert not overheat


# --- run_region cleanup on abnormal exit ----------------------------------

class _FakeProc:
    """Minimal stand-in for a Popen fio process."""

    def __init__(self, returncode=0):
        self._alive = True
        self._rc = returncode
        self.stdout = io.StringIO("")  # empty: drain thread does nothing
        self.terminated = False

    def poll(self):
        return None if self._alive else self._rc

    def wait(self, timeout=None):
        self._alive = False
        return self._rc

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.terminated = True
        self._alive = False


def test_run_region_terminates_fio_if_monitor_errors(tmp_path):
    # If anything throws mid-region, the finally must kill fio so no write is
    # left running against the device (parity with the shell's INT/TERM trap).
    proc = _FakeProc()

    def boom():
        raise RuntimeError("temperature source failed")

    runner = FioRunner(
        read_temp=boom,
        policy=POLICY,
        sleep=lambda _s: None,
        popen=lambda _argv: proc,  # type: ignore[arg-type,return-value]
        echo=lambda _line: None,
    )
    with pytest.raises(RuntimeError):
        runner.run_region("/dev/sdx", Region(1, 0, 1024), tmp_path / "f.log")
    assert proc.terminated


def test_run_region_returns_overheat_when_monitor_kills_on_ceiling(tmp_path):
    # End-to-end: a ceiling temperature makes the monitor kill fio and the region
    # is classified OVERHEAT - the run_region wiring, not just the isolated monitor.
    proc = _FakeProc(returncode=143)  # SIGTERM-ish; classify ignores it on overheat
    runner = FioRunner(
        read_temp=lambda: POLICY.ceiling_c,
        policy=POLICY,
        sleep=lambda _s: None,
        popen=lambda _argv: proc,  # type: ignore[arg-type,return-value]
        echo=lambda _line: None,
    )
    result = runner.run_region("/dev/sdx", Region(1, 0, 1024), tmp_path / "f.log")
    assert result is RegionResult.OVERHEAT
    assert proc.terminated


def test_run_region_drains_all_output_before_closing_log_on_error(tmp_path):
    # Even when the monitor errors, the drain thread is joined before the log
    # file closes, so all streamed output lands and none is lost to a closed sink.
    proc = _FakeProc()
    proc.stdout = io.StringIO("line1\nline2\n")

    def boom():
        raise RuntimeError("temperature source failed")

    runner = FioRunner(
        read_temp=boom,
        policy=POLICY,
        sleep=lambda _s: None,
        popen=lambda _argv: proc,  # type: ignore[arg-type,return-value]
        echo=lambda _line: None,
    )
    log = tmp_path / "f.log"
    with pytest.raises(RuntimeError):
        runner.run_region("/dev/sdx", Region(1, 0, 1024), log)
    assert proc.terminated
    assert log.read_text() == "line1\nline2\n"


class _FinishingProc:
    """A fio stand-in that stays alive for ``alive_polls`` monitor iterations and
    then exits with ``returncode`` on its own (no kill needed).
    """

    def __init__(self, returncode=0, alive_polls=2, stdout=""):
        self._rc = returncode
        self._polls = alive_polls
        self.stdout = io.StringIO(stdout)
        self.terminated = False

    def poll(self):
        if self._polls > 0:
            self._polls -= 1
            return None
        return self._rc

    def wait(self, timeout=None):
        self._polls = 0
        return self._rc

    def terminate(self):
        self.terminated = True
        self._polls = 0

    def kill(self):
        self.terminated = True
        self._polls = 0


class _StubbornProc:
    """A fio stand-in that ignores SIGTERM: ``terminate`` leaves it alive and
    ``wait(timeout=...)`` times out, so :meth:`FioRunner._terminate` must escalate
    to ``kill`` (SIGKILL).
    """

    def __init__(self):
        self.stdout = io.StringIO("")
        self.terminate_called = False
        self.killed = False
        self._alive = True

    def poll(self):
        return None if self._alive else -9

    def wait(self, timeout=None):
        if timeout is not None and self._alive:
            raise subprocess.TimeoutExpired(cmd="fio", timeout=timeout)
        return -9

    def terminate(self):
        self.terminate_called = True  # but the process stays alive

    def kill(self):
        self.killed = True
        self._alive = False


def _runner_for(proc, tmp_path, *, read_temp=lambda: 30, echo=lambda _line: None):
    return FioRunner(
        read_temp=read_temp,
        policy=POLICY,
        sleep=lambda _s: None,
        popen=lambda _argv: proc,  # type: ignore[arg-type,return-value]
        echo=echo,
    )


def test_run_region_fail_when_fio_exits_nonzero_while_cool(tmp_path):
    # A verify mismatch: fio finishes cool with a non-zero exit -> FAIL, and since
    # it exited on its own the process is never terminated.
    proc = _FinishingProc(returncode=1)
    runner = _runner_for(proc, tmp_path)
    result = runner.run_region("/dev/sdx", Region(1, 0, 1024), tmp_path / "f.log")
    assert result is RegionResult.FAIL
    assert not proc.terminated


def test_run_region_escalates_to_sigkill_when_fio_ignores_sigterm(tmp_path):
    # A ceiling breach kills fio, but it ignores SIGTERM; _terminate must escalate
    # to SIGKILL rather than hang, and the region is classified OVERHEAT.
    proc = _StubbornProc()
    runner = _runner_for(proc, tmp_path, read_temp=lambda: POLICY.ceiling_c)
    result = runner.run_region("/dev/sdx", Region(1, 0, 1024), tmp_path / "f.log")
    assert result is RegionResult.OVERHEAT
    assert proc.terminate_called and proc.killed


def test_run_region_does_not_launch_fio_if_log_open_fails(tmp_path):
    # The evidence log is opened before fio starts; if that open fails we must not
    # have launched a destructive write with nothing to drain or stop it.
    launched = []

    def popen(_argv):
        launched.append(True)
        return _FinishingProc(0)

    runner = FioRunner(
        read_temp=lambda: 30, policy=POLICY, sleep=lambda _s: None,
        popen=popen,  # type: ignore[arg-type]  # test fake stands in for Popen
        echo=lambda _line: None,
    )
    bad_log = tmp_path / "missing_dir" / "f.log"  # parent doesn't exist -> open fails
    with pytest.raises(FileNotFoundError):
        runner.run_region("/dev/sdx", Region(1, 0, 1024), bad_log)
    assert launched == []  # fio was never started


def test_run_region_raises_when_output_drain_fails(tmp_path):
    # If the drain thread can't mirror fio's output (e.g. the log disk fills), the
    # evidence log is incomplete, so run_region must raise rather than report PASS.
    proc = _FinishingProc(returncode=0, stdout="line1\nline2\n")

    def boom_echo(_line):
        raise RuntimeError("log disk full")

    runner = _runner_for(proc, tmp_path, echo=boom_echo)
    with pytest.raises(RuntimeError, match="log disk full"):
        runner.run_region("/dev/sdx", Region(1, 0, 1024), tmp_path / "f.log")


# --- real fio integration (happy path) ------------------------------------

@pytest.mark.fio
@pytest.mark.skipif(shutil.which("fio") is None, reason="fio not installed")
def test_run_region_writeverify_on_scratch_file(tmp_path):
    scratch = tmp_path / "scratch.bin"
    scratch.write_bytes(b"\0" * (8 * MIB))
    log = tmp_path / "fio.log"
    sleep, _ = collect_sleep()
    runner = FioRunner(
        read_temp=lambda: 30,          # always cool
        policy=POLICY,
        sleep=sleep,
        popen=default_popen,           # the real subprocess, for this integration test
        echo=lambda line: None,
    )
    result = runner.run_region(str(scratch), Region(1, 0, 8 * MIB), log)
    assert result is RegionResult.PASS
    assert log.exists() and log.stat().st_size > 0
