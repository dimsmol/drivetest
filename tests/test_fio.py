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
    TERMINATE_GRACE_S,
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
    argv = build_writeverify_argv("/dev/sdx", 1024, 2048)
    assert "--verify=crc32c" in argv
    assert "--do_verify=1" in argv
    assert "--verify_fatal=1" in argv
    assert "--offset=1024" in argv
    assert "--size=2048" in argv
    assert "--filename=/dev/sdx" in argv
    assert "--bs=1M" in argv  # default block size


def test_writeverify_argv_uses_given_bs():
    # The tail pass overrides the default 1 MiB block with the remainder size.
    argv = build_writeverify_argv("/dev/sdx", 8192, 512, bs="512")
    assert "--bs=512" in argv
    assert "--offset=8192" in argv
    assert "--size=512" in argv


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


def test_parse_read_json_fails_closed_on_non_object_json():
    # Valid-but-non-object fio JSON (null, a list, a number) must fail closed as a
    # plain ValueError - not a bare AttributeError that escapes the caller's
    # `except ValueError` and aborts the whole benchmark battery.
    for bad in (None, [], 5, "x"):
        with pytest.raises(ValueError) as exc:
            parse_read_json(bad, ReadKind.SEQ)
        assert not isinstance(exc.value, FioReadError)


def test_parse_read_json_fails_closed_on_non_object_job():
    # jobs[0] present but not an object (a bare number): .get would raise, so fail
    # closed as ValueError rather than propagate an AttributeError.
    with pytest.raises(ValueError):
        parse_read_json({"jobs": [5]}, ReadKind.SEQ)


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
        self.wait_calls: list[float | None] = []
        self._alive = True

    def poll(self):
        return None if self._alive else -9

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
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


# --- body + tail coverage of a sub-MiB region -----------------------------

class _RecordingPopen:
    """A ``popen`` stand-in that hands out queued procs and records each argv, so
    a test can assert how many fio invocations run and with what parameters.
    """

    def __init__(self, procs):
        self._procs = list(procs)
        self.argvs: list[list[str]] = []

    def __call__(self, argv):
        self.argvs.append(argv)
        return self._procs.pop(0)


def _recording_runner(popen, tmp_path):
    return FioRunner(
        read_temp=lambda: 30,  # cool: monitor never kills
        policy=POLICY,
        sleep=lambda _s: None,
        popen=popen,  # type: ignore[arg-type]
        echo=lambda _line: None,
    )


def test_run_region_single_pass_when_mib_aligned(tmp_path):
    # A whole-MiB region is one fio pass at bs=1M - no tail.
    popen = _RecordingPopen([_FinishingProc(returncode=0)])
    runner = _recording_runner(popen, tmp_path)
    result = runner.run_region("/dev/sdx", Region(1, 0, 4 * MIB), tmp_path / "f.log")
    assert result is RegionResult.PASS
    assert len(popen.argvs) == 1
    assert "--bs=1M" in popen.argvs[0]
    assert f"--size={4 * MIB}" in popen.argvs[0]


def test_run_region_writes_body_then_tail_for_sub_mib_remainder(tmp_path):
    # A region whose size is not a whole MiB runs as a 1 MiB body plus a final
    # short block, so the sub-MiB tail (the last bytes of a device) is verified.
    tail = 90112  # 88 KiB, sector-aligned - like a real end-of-drive remainder
    region = Region(2, 5 * MIB, 3 * MIB + tail)
    popen = _RecordingPopen([_FinishingProc(returncode=0), _FinishingProc(returncode=0)])
    runner = _recording_runner(popen, tmp_path)
    result = runner.run_region("/dev/sdx", region, tmp_path / "f.log")
    assert result is RegionResult.PASS
    assert len(popen.argvs) == 2
    body, tail_argv = popen.argvs
    assert "--bs=1M" in body
    assert f"--offset={5 * MIB}" in body and f"--size={3 * MIB}" in body
    # The tail starts right after the body and covers exactly the remainder in one IO.
    assert f"--offset={5 * MIB + 3 * MIB}" in tail_argv
    assert f"--size={tail}" in tail_argv and f"--bs={tail}" in tail_argv


def test_run_region_tail_only_when_region_below_one_mib(tmp_path):
    # A region smaller than a MiB has no body - just the tail pass covers it whole.
    popen = _RecordingPopen([_FinishingProc(returncode=0)])
    runner = _recording_runner(popen, tmp_path)
    result = runner.run_region("/dev/sdx", Region(1, 0, 4096), tmp_path / "f.log")
    assert result is RegionResult.PASS
    assert len(popen.argvs) == 1
    assert f"--size={4096}" in popen.argvs[0] and f"--bs={4096}" in popen.argvs[0]


def test_run_region_body_failure_short_circuits_tail(tmp_path):
    # A verify mismatch in the body must stop the region there - the tail never runs.
    popen = _RecordingPopen([_FinishingProc(returncode=1)])  # body FAILs
    runner = _recording_runner(popen, tmp_path)
    result = runner.run_region("/dev/sdx", Region(2, 0, 3 * MIB + 90112), tmp_path / "f.log")
    assert result is RegionResult.FAIL
    assert len(popen.argvs) == 1  # tail skipped


def test_run_region_body_and_tail_output_share_one_log(tmp_path):
    # Both passes stream to the same open log; the tail must append, not truncate
    # the body's output (a guard against a future refactor reopening the file).
    body = _FinishingProc(returncode=0, stdout="BODY-OUTPUT\n")
    tail = _FinishingProc(returncode=0, stdout="TAIL-OUTPUT\n")
    popen = _RecordingPopen([body, tail])
    runner = _recording_runner(popen, tmp_path)
    log = tmp_path / "f.log"
    result = runner.run_region("/dev/sdx", Region(1, 0, 2 * MIB + 4096), log)
    assert result is RegionResult.PASS
    text = log.read_text()
    assert "BODY-OUTPUT" in text and "TAIL-OUTPUT" in text


def test_run_region_rejects_nonpositive_size(tmp_path):
    # A zero-size region must fail closed, not fall through as a no-op PASS.
    popen = _RecordingPopen([])
    runner = _recording_runner(popen, tmp_path)
    with pytest.raises(ValueError):
        runner.run_region("/dev/sdx", Region(1, 0, 0), tmp_path / "f.log")
    assert len(popen.argvs) == 0  # no fio launched


def test_run_region_escalates_to_sigkill_when_fio_ignores_sigterm(tmp_path):
    # A ceiling breach kills fio, but it ignores SIGTERM; _terminate must escalate
    # to SIGKILL rather than hang, and the region is classified OVERHEAT.
    proc = _StubbornProc()
    runner = _runner_for(proc, tmp_path, read_temp=lambda: POLICY.ceiling_c)
    result = runner.run_region("/dev/sdx", Region(1, 0, 1024), tmp_path / "f.log")
    assert result is RegionResult.OVERHEAT
    assert proc.terminate_called and proc.killed


def test_terminate_reaps_process_after_sigkill():
    # After escalating to SIGKILL, _terminate must reap the process with a blocking
    # wait() so it can't linger as a zombie on paths that never wait() again (the
    # exception-cleanup finally). The reap is a no-timeout wait after the kill.
    proc = _StubbornProc()
    FioRunner._terminate(proc)  # type: ignore[arg-type]  # test fake stands in for Popen
    assert proc.terminate_called and proc.killed
    # First wait was the grace wait (timed out -> escalation); the last is the reap.
    assert proc.wait_calls[0] == TERMINATE_GRACE_S
    assert proc.wait_calls[-1] is None


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


@pytest.mark.fio
@pytest.mark.skipif(shutil.which("fio") is None, reason="fio not installed")
def test_run_region_writeverify_covers_sub_mib_tail_on_scratch_file(tmp_path):
    # A region that is not a whole number of MiB: real fio must write+verify both
    # the 1 MiB body and the trailing short block, so the whole region PASSes. This
    # is the regression guard - a 1 MiB-only job silently drops this tail.
    tail = 88 * KIB  # 90112 B, 512- and 4 KiB-aligned for --direct
    size = 8 * MIB + tail
    scratch = tmp_path / "scratch.bin"
    scratch.write_bytes(b"\0" * size)
    log = tmp_path / "fio.log"
    sleep, _ = collect_sleep()
    runner = FioRunner(
        read_temp=lambda: 30,
        policy=POLICY,
        sleep=sleep,
        popen=default_popen,
        echo=lambda line: None,
    )
    result = runner.run_region(str(scratch), Region(1, 0, size), log)
    assert result is RegionResult.PASS
