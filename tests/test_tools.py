"""Tests for external-tool presence checks."""

from __future__ import annotations

from drivetest.tools import BASE_TOOLS, is_nvme_target, missing_tools, required_tools


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


def test_is_nvme_target_follows_symlink_to_real_nvme_node(tmp_path):
    # A by-id/by-path symlink whose own name isn't "nvme..." but which resolves to
    # a real nvme node must be detected as NVMe (realpath resolution is the point).
    real = tmp_path / "nvme0n1"
    real.touch()
    link = tmp_path / "disk-by-id-XYZ"
    link.symlink_to(real)
    assert is_nvme_target(str(link))


def test_is_nvme_target_symlink_name_nvme_but_target_scsi_is_false(tmp_path):
    # The reverse: a link *named* like nvme that resolves to a SCSI node is not
    # NVMe - realpath, not the surface name, decides.
    real = tmp_path / "sda"
    real.touch()
    link = tmp_path / "nvme-lookalike"
    link.symlink_to(real)
    assert not is_nvme_target(str(link))
