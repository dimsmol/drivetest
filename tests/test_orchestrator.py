"""Integration tests for the end-to-end run, driven by a fake command runner.

These exercise the wiring and exit codes for the read-only happy path and the
safety-refusal path without touching hardware or running real commands.
"""

from __future__ import annotations

import io
import json

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

from .conftest import FakeRunner, load_text


def test_region_to_verify_mapping_is_exhaustive():
    # Every fio region result must map to a verify status, or a write phase
    # would KeyError at runtime.
    assert set(_REGION_TO_VERIFY) == set(RegionResult)


@pytest.fixture(autouse=True)
def _no_missing_tools(monkeypatch):  # pyright: ignore[reportUnusedFunction]  # autouse: pytest calls it
    # Decouple tests from what happens to be installed on the host.
    monkeypatch.setattr("drivetest.orchestrator.missing_tools", lambda required: [])


def _config(device, *, write=False, assume_yes=False, quick=False, force=False) -> RunConfig:
    """A RunConfig for these tests, defaulting the knobs a run doesn't vary."""
    return RunConfig(
        device=device,
        write=write,
        quick=quick,
        force=force,
        only=None,
        assume_yes=assume_yes,
        log_dir=None,
        parts=DEFAULT_PARTS,
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


class _SequencedSmartRunner(FakeRunner):
    """A FakeRunner whose ``smartctl --json`` calls return a scripted sequence,
    so the before/after health snapshots can differ.
    """

    def __init__(self, json_sequence):
        super().__init__()
        self._seq = iter(json_sequence)

    def run(self, argv, *, input=None, timeout=None):
        argv_list = list(argv)
        if argv_list[0] == "smartctl" and "--json" in argv_list:
            self.calls.append(argv_list)
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
            self.calls.append(argv_list)
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
