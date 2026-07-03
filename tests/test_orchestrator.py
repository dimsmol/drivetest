"""Integration tests for the end-to-end run, driven by a fake command runner.

These exercise the wiring and exit codes for the read-only happy path and the
safety-refusal path without touching hardware or running real commands.
"""

from __future__ import annotations

import pytest

from drivetest.cli import Options
from drivetest.orchestrator import EXIT_ATTENTION, EXIT_OK, EXIT_REFUSED, RunContext, run
from drivetest.thermal import ThermalPolicy

from .conftest import FakeRunner, load_text


@pytest.fixture(autouse=True)
def _no_missing_tools(monkeypatch):  # pyright: ignore[reportUnusedFunction]  # autouse: pytest calls it
    # Decouple tests from what happens to be installed on the host.
    monkeypatch.setattr("drivetest.orchestrator.missing_tools", lambda required: [])


def _ctx(runner, tmp_path, **kw) -> RunContext:
    return RunContext(
        runner=runner,
        workdir=tmp_path,
        stamp="TEST",
        sleep=lambda _s: None,
        confirm=kw.pop("confirm", lambda _p: ""),
        policy=kw.pop("policy", ThermalPolicy()),
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

    code = run(Options(device="/dev/sda", write=False), _ctx(runner, tmp_path))
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
    code = run(Options(device="/dev/nvme0n1", write=True, assume_yes=False), ctx)

    assert code == EXIT_REFUSED
    summary = (tmp_path / "drive_test_S3ZHNF0KC28756_TEST" / "summary.log").read_text()
    assert "refusing to write" in summary
    assert "not-system-disk" in summary
    # never reached the destructive confirmation
    assert confirmed == []


def test_device_not_found(tmp_path):
    runner = FakeRunner()
    runner.add("lsblk", contains=["-Jb"], stdout='{"blockdevices": []}')
    code = run(Options(device="/dev/nope", write=False), _ctx(runner, tmp_path))
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
            from drivetest.proc import Result

            self.calls.append(argv_list)
            return Result(tuple(argv_list), 0, next(self._seq), "")
        return super().run(argv, input=input, timeout=timeout)


def test_readonly_flags_worsened_smart(tmp_path):
    # after-snapshot shows new media errors -> ATTENTION even without a write.
    import json

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

    code = run(Options(device="/dev/sda", write=False), _ctx(runner, tmp_path))
    assert code == EXIT_ATTENTION
