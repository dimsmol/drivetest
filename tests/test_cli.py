"""Tests for argument parsing and validation."""

from __future__ import annotations

import pytest

from drivetest.cli import parse_args


def test_minimal_readonly():
    opts = parse_args(["/dev/sdb"])
    assert opts.device == "/dev/sdb"
    assert not opts.write
    assert opts.parts == 1


def test_write_parts_only():
    opts = parse_args(["--write", "--parts", "8", "--only", "1-4", "/dev/sdb"])
    assert opts.write
    assert opts.parts == 8
    assert opts.only == "1-4"


def test_quick_requires_write():
    with pytest.raises(SystemExit):
        parse_args(["--quick", "/dev/sdb"])


def test_only_requires_write():
    with pytest.raises(SystemExit):
        parse_args(["--only", "1-4", "/dev/sdb"])


def test_only_rejects_quick_combo():
    with pytest.raises(SystemExit):
        parse_args(["--write", "--quick", "--only", "1", "/dev/sdb"])


def test_only_spec_validated_against_parts():
    with pytest.raises(SystemExit):
        parse_args(["--write", "--parts", "4", "--only", "5", "/dev/sdb"])


def test_parts_must_be_positive():
    with pytest.raises(SystemExit):
        parse_args(["--write", "--parts", "0", "/dev/sdb"])


def test_force_requires_write():
    with pytest.raises(SystemExit):
        parse_args(["--force", "/dev/sdb"])


def test_device_required():
    with pytest.raises(SystemExit):
        parse_args([])


def test_assume_yes_and_log_dir():
    opts = parse_args(["--write", "--assume-yes", "--log-dir", "/tmp/logs", "/dev/sdb"])
    assert opts.assume_yes
    assert opts.log_dir == "/tmp/logs"
