"""Tests for the destructive-write guards - the most safety-critical logic."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from drivetest.devices import Device, parse_lsblk
from drivetest.safety import (
    BlankProbe,
    Check,
    RootInfo,
    blocking_failures,
    check_blank,
    check_identity_stable,
    check_not_mounted,
    check_not_system_disk,
    check_serial_unique,
    check_whole_disk,
    evaluate_write_safety,
)

from .conftest import load_text


def _disk(**over) -> Device:
    base = dict(
        path="/dev/sdx", name="sdx", type="disk", size=100,
        serial="UNIQUE", model="M", wwn=None, tran="usb",
    )
    base.update(over)
    return Device(**base)  # type: ignore[arg-type]


# --- whole disk -----------------------------------------------------------

def test_partition_is_rejected():
    part = _disk(type="part", name="sdx1", path="/dev/sdx1")
    assert not check_whole_disk(part).ok


def test_whole_disk_accepted():
    assert check_whole_disk(_disk()).ok


# --- mounted --------------------------------------------------------------

def test_mounted_child_is_rejected():
    [nvme] = parse_lsblk(load_text("lsblk_nvme_system.json"))
    check = check_not_mounted(nvme)
    assert not check.ok
    assert "/" in check.detail


def test_unmounted_disk_accepted():
    [usb] = parse_lsblk(load_text("lsblk_usb_sda.json"))
    assert check_not_mounted(usb).ok


# --- system disk ----------------------------------------------------------

def test_disk_backing_root_is_rejected():
    dev = _disk(name="nvme0n1", path="/dev/nvme0n1")
    root = RootInfo(source="/dev/nvme0n1p4", parent_disks=("nvme0n1p4", "nvme0n1"))
    assert not check_not_system_disk(dev, root).ok


def test_disk_not_backing_root_accepted():
    dev = _disk(name="sda", path="/dev/sda")
    root = RootInfo(source="/dev/nvme0n1p4", parent_disks=("nvme0n1p4", "nvme0n1"))
    assert check_not_system_disk(dev, root).ok


def test_unresolved_root_passes_but_flags_uncertainty():
    dev = _disk(name="sda", path="/dev/sda")
    root = RootInfo(source="zfs/root", resolved=False)
    check = check_not_system_disk(dev, root)
    assert check.ok
    assert "cannot resolve" in check.detail


# --- blank ----------------------------------------------------------------

def test_blank_disk_accepted():
    assert check_blank(_disk(), BlankProbe()).ok


def test_disk_with_signature_rejected():
    probe = BlankProbe(signatures=("ntfs",))
    check = check_blank(_disk(), probe)
    assert not check.ok
    assert "ntfs" in check.detail


def test_blank_probe_error_fails_closed():
    # A probe that errored must be treated as non-blank, never "looks empty".
    probe = BlankProbe(probe_error=True)
    assert not probe.is_blank
    assert not check_blank(_disk(), probe).ok


def test_holders_or_children_reject():
    assert not check_blank(_disk(), BlankProbe(holders=("dm-0",))).ok
    assert not check_blank(_disk(), BlankProbe(children=("sdx1",))).ok


# --- serial uniqueness ----------------------------------------------------

def test_missing_serial_rejected():
    assert not check_serial_unique(_disk(serial=None), []).ok


def test_empty_string_serial_rejected():
    # A cheap USB bridge reporting a blank serial must fail closed, same as None.
    assert not check_serial_unique(_disk(serial=""), []).ok


def test_duplicate_serial_rejected():
    dev = _disk(serial="DUP")
    assert not check_serial_unique(dev, ["DUP", "DUP", "OTHER"]).ok


def test_unique_serial_accepted():
    dev = _disk(serial="UNIQUE")
    assert check_serial_unique(dev, ["UNIQUE", "OTHER"]).ok


# --- identity stability ---------------------------------------------------

def test_identity_change_detected():
    # Same serial/wwn/model, different size -> reassigned node.
    assert not check_identity_stable(("A", "B", 1, "M"), ("A", "B", 2, "M")).ok


def test_identity_unchanged():
    assert check_identity_stable(("A", "B", 1, "M"), ("A", "B", 1, "M")).ok


def test_identity_distinguishes_none_from_empty():
    # The tuple form keeps a missing field distinct from an empty string, which a
    # delimiter-joined string could have collided.
    assert not check_identity_stable((None, None, 1, "M"), ("", "", 1, "M")).ok


# --- composition ----------------------------------------------------------

def test_evaluate_all_pass_for_a_good_new_drive():
    dev = _disk(serial="UNIQUE")
    checks = evaluate_write_safety(
        dev,
        root=RootInfo(source="/dev/nvme0n1p4", parent_disks=("nvme0n1",)),
        probe=BlankProbe(),
        all_serials=["UNIQUE"],
    )
    assert blocking_failures(checks) == []


def test_force_downgrades_only_blank():
    dev = _disk(serial="UNIQUE", type="part")  # whole-disk fails
    checks = evaluate_write_safety(
        dev,
        # An established root that isn't this device: force may downgrade blank.
        root=RootInfo(source="/dev/nvme0n1p4", parent_disks=("nvme0n1",)),
        probe=BlankProbe(signatures=("ext4",)),  # blank fails
        all_serials=["UNIQUE"],
        force=True,
    )
    failures = {c.name for c in blocking_failures(checks)}
    # blank was forced to pass, but the whole-disk failure still blocks
    assert "blank" not in failures
    assert "whole-disk" in failures


def test_force_does_not_downgrade_blank_when_root_unresolved():
    # With the system disk unresolvable (ZFS/overlay root), the blank guard is
    # the only thing standing between --force and the live system disk, so force
    # must NOT downgrade it.
    dev = _disk(serial="UNIQUE")
    checks = evaluate_write_safety(
        dev,
        root=RootInfo(source="zfs/root", resolved=False),
        probe=BlankProbe(signatures=("ext4",)),  # non-blank
        all_serials=["UNIQUE"],
        force=True,
    )
    failures = {c.name for c in blocking_failures(checks)}
    assert "blank" in failures


def test_force_does_not_downgrade_blank_when_root_walk_empty():
    # resolved=True but no parent disks == not actually established; same risk as
    # an unresolved root, so force must not downgrade blank here either.
    dev = _disk(serial="UNIQUE")
    checks = evaluate_write_safety(
        dev,
        root=RootInfo(source="/dev/x", parent_disks=(), resolved=True),
        probe=BlankProbe(signatures=("ext4",)),
        all_serials=["UNIQUE"],
        force=True,
    )
    failures = {c.name for c in blocking_failures(checks)}
    assert "blank" in failures


def test_empty_parent_disks_is_uncertain_not_clean():
    # A resolved root that names no disk cannot clear a target as "not the system
    # disk"; the guard must flag uncertainty rather than pass cleanly.
    dev = _disk(name="sda", path="/dev/sda")
    check = check_not_system_disk(dev, RootInfo(source="/dev/x", parent_disks=(), resolved=True))
    assert check.ok  # non-blocking, but...
    assert "cannot resolve" in check.detail  # ...flagged as uncertain, not "does not back /"


def test_force_does_not_bypass_mount_or_system_guards():
    dev = _disk(name="nvme0n1", path="/dev/nvme0n1", serial="UNIQUE")
    checks = evaluate_write_safety(
        dev,
        root=RootInfo(source="/dev/nvme0n1p4", parent_disks=("nvme0n1",)),
        probe=BlankProbe(signatures=("ext4",)),
        all_serials=["UNIQUE"],
        force=True,
    )
    failures = {c.name for c in blocking_failures(checks)}
    assert "not-system-disk" in failures


def test_check_is_an_immutable_value_object():
    c = Check("x", True, "d")
    assert c == Check("x", True, "d")  # compares by value
    with pytest.raises(FrozenInstanceError):
        c.ok = False  # type: ignore[misc]  # frozen: assignment must raise
