"""Tests for the shared FakeRunner harness itself.

Many tests depend on it, so its contract (call recording, rule matching, and
simulating a failing tool) is worth guarding directly.
"""

from __future__ import annotations

import pytest

from .conftest import Call, FakeRunner


def test_records_argv_input_and_timeout():
    runner = FakeRunner()
    runner.add("smartctl", contains=["-x"], stdout="ok")
    runner.run(["smartctl", "-x", "/dev/sda"], input="serial\n", timeout=5.0)
    assert runner.calls == [Call(("smartctl", "-x", "/dev/sda"), "serial\n", 5.0)]


def test_rule_can_raise_to_simulate_a_missing_tool():
    # The real runner raises FileNotFoundError when a tool isn't installed; a
    # rule can reproduce that so error handling becomes testable.
    runner = FakeRunner()
    runner.add("nvme", contains=["smart-log"], error=FileNotFoundError("nvme"))
    with pytest.raises(FileNotFoundError):
        runner.run(["nvme", "smart-log", "/dev/nvme0n1"])
    # the attempted call is still recorded
    assert runner.calls[-1].argv == ("nvme", "smart-log", "/dev/nvme0n1")


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
