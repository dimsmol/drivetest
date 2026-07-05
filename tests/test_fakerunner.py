"""Tests for the shared FakeRunner harness itself.

Many tests depend on it, so its contract (call recording, rule matching, and
simulating a failing tool) is worth guarding directly.
"""

from __future__ import annotations

import subprocess

import pytest

from drivetest.proc import ProcTimeout, ToolUnavailable

from .conftest import Call, FakeRunner


def test_records_argv_input_and_timeout():
    runner = FakeRunner()
    runner.add("smartctl", contains=["-x"], stdout="ok")
    runner.run(["smartctl", "-x", "/dev/sda"], input="serial\n", timeout=5.0)
    assert runner.calls == [Call(("smartctl", "-x", "/dev/sda"), "serial\n", 5.0)]


def test_rule_can_raise_to_simulate_a_missing_tool():
    # A missing tool surfaces as ToolUnavailable, exactly like the real runner: a
    # raw FileNotFoundError is translated, so callers test the type they see.
    runner = FakeRunner()
    runner.add("nvme", contains=["smart-log"], error=FileNotFoundError("nvme"))
    with pytest.raises(ToolUnavailable):
        runner.run(["nvme", "smart-log", "/dev/nvme0n1"])
    # the attempted call is still recorded
    assert runner.calls[-1].argv == ("nvme", "smart-log", "/dev/nvme0n1")


def test_rule_can_raise_to_simulate_a_timeout():
    # A raw TimeoutExpired is likewise translated to ProcTimeout.
    runner = FakeRunner()
    runner.add("fio", contains=["--name"], error=subprocess.TimeoutExpired("fio", 5.0))
    with pytest.raises(ProcTimeout):
        runner.run(["fio", "--name", "wr"], timeout=5.0)


def test_rule_can_raise_oserror_to_simulate_a_non_executable_tool():
    # A non-executable tool raises PermissionError (an OSError subclass) from the
    # real runner and is translated to ToolUnavailable; the fake must mirror that, so
    # higher-layer tests see the same type production does.
    runner = FakeRunner()
    runner.add("smartctl", contains=["-x"], error=PermissionError("smartctl"))
    with pytest.raises(ToolUnavailable) as excinfo:
        runner.run(["smartctl", "-x", "/dev/sda"])
    # the cause is preserved, mirroring the real runner
    assert isinstance(excinfo.value.cause, PermissionError)


def test_rules_tried_in_registration_order():
    runner = FakeRunner()
    runner.add("smartctl", contains=["-i", "sat"], stdout="specific")
    runner.add("smartctl", contains=["-i"], stdout="general")
    assert runner.run(["smartctl", "-i", "-d", "sat", "/dev/sda"]).stdout == "specific"
    assert runner.run(["smartctl", "-i", "/dev/sda"]).stdout == "general"


def test_unmatched_command_raises_assertion():
    runner = FakeRunner()
    with pytest.raises(AssertionError):
        runner.run(["lsblk", "-Jb"])
