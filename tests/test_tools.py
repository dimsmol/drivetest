"""Tests for external-tool presence checks."""

from __future__ import annotations

from drivetest.tools import BASE_TOOLS, missing_tools, required_tools


def test_required_tools_adds_nvme_only_for_nvme_target():
    assert "nvme" in required_tools("/dev/nvme0n1")
    assert "nvme" in required_tools("/dev/nvme0n1p2")
    assert "nvme" not in required_tools("/dev/sda")
    for tool in BASE_TOOLS:
        assert tool in required_tools("/dev/sda")


def test_required_tools_nvme_detection_ignores_unrelated_path_components():
    # A non-nvme node whose parent dir merely contains "nvme" must not pull in
    # the nvme tool (the old substring check would have false-matched here).
    assert "nvme" not in required_tools("/dev/nvme-enclosure/sdb")


def test_missing_tools_reports_all_absent_in_input_order():
    present = {"lsblk", "fio"}
    which = lambda name: "/usr/bin/" + name if name in present else None  # noqa: E731
    missing = missing_tools(["lsblk", "fio", "smartctl", "wipefs"], which=which)
    assert missing == ["smartctl", "wipefs"]  # order preserved for a stable message


def test_missing_tools_empty_when_all_present():
    assert missing_tools(["a", "b"], which=lambda _n: "/bin/x") == []
