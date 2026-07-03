"""Tests for fio argv building, JSON parsing, region classification and the
temperature monitor. A real-fio integration test covers the happy write path.
"""

from __future__ import annotations

import io
import shutil

import pytest

from drivetest.fio import (
    FioRunner,
    RegionResult,
    build_read_argv,
    build_writeverify_argv,
    classify_region,
    monitor_region,
    parse_read_json,
)
from drivetest.planning import Region
from drivetest.thermal import ThermalPolicy

from .conftest import collect_sleep, load_json

POLICY = ThermalPolicy()


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
    seq = build_read_argv("/dev/sdx", "seqread")
    assert "--bs=1M" in seq and "--rw=read" in seq and "--output-format=json" in seq
    rnd = build_read_argv("/dev/sdx", "randread")
    assert "--bs=4k" in rnd and "--rw=randread" in rnd


def test_read_argv_rejects_unknown_kind():
    with pytest.raises(ValueError):
        build_read_argv("/dev/sdx", "bogus")


# --- JSON parsing ---------------------------------------------------------

def test_parse_seqread_json():
    stats = parse_read_json(load_json("fio_seqread.json"), "seqread")
    assert stats.bw_bytes == 1002438656
    assert round(stats.bw_mb) == 1002
    assert round(stats.iops) == 956


def test_parse_randread_json():
    stats = parse_read_json(load_json("fio_randread.json"), "randread")
    assert round(stats.iops) == 69905


def test_parse_read_json_falls_back_to_kib_bw():
    # older fio without bw_bytes
    obj = {"jobs": [{"read": {"bw": 1000, "iops": 10}}]}
    stats = parse_read_json(obj, "seqread")
    assert stats.bw_bytes == 1000 * 1024


def test_parse_read_json_no_jobs():
    with pytest.raises(ValueError):
        parse_read_json({"jobs": []}, "seqread")


# --- region classification ------------------------------------------------

def test_classify_region():
    assert classify_region(False, 0) is RegionResult.PASS
    assert classify_region(False, 1) is RegionResult.FAIL
    assert classify_region(True, 0) is RegionResult.OVERHEAT
    assert classify_region(True, 137) is RegionResult.OVERHEAT  # overheat wins


# --- temperature monitor --------------------------------------------------

def test_monitor_kills_on_ceiling_breach():
    temps = iter([50, 60, 78])   # third sample hits the ceiling
    killed = []
    sleep, _ = collect_sleep()
    overheat = monitor_region(
        is_alive=lambda: True,
        read_temp=lambda: next(temps),
        policy=POLICY,
        sleep=sleep,
        kill=lambda: killed.append(True),
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


# --- real fio integration (happy path) ------------------------------------

@pytest.mark.fio
@pytest.mark.skipif(shutil.which("fio") is None, reason="fio not installed")
def test_run_region_writeverify_on_scratch_file(tmp_path):
    scratch = tmp_path / "scratch.bin"
    scratch.write_bytes(b"\0" * (8 * 1024 * 1024))  # 8 MiB
    log = tmp_path / "fio.log"
    sleep, _ = collect_sleep()
    runner = FioRunner(
        read_temp=lambda: 30,          # always cool
        policy=POLICY,
        sleep=sleep,
        echo=lambda line: None,
    )
    result = runner.run_region(str(scratch), Region(1, 0, 8 * 1024 * 1024), log)
    assert result is RegionResult.PASS
    assert log.exists() and log.stat().st_size > 0
