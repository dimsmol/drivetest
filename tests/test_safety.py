"""Tests for the destructive-write guards - the most safety-critical logic."""

from __future__ import annotations

from collections.abc import Sequence
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
        mountpoints=(), children=(),
    )
    base.update(over)
    return Device(**base)  # type: ignore[arg-type]


def _probe(**over) -> BlankProbe:
    """A BlankProbe defaulting to blank/no-error; a test overrides only the fields
    it cares about. Production always sets all four explicitly, so they have no
    real defaults - this helper holds the test-only convenience.
    """
    base = dict(holders=(), signatures=(), children=(), probe_error=False)
    base.update(over)
    return BlankProbe(**base)  # type: ignore[arg-type]


def _root(**over) -> RootInfo:
    """A RootInfo defaulting to an established, unrelated root; overridden per test."""
    base = dict(source="/dev/nvme0n1p4", parent_disks=("nvme0n1",), resolved=True)
    base.update(over)
    return RootInfo(**base)  # type: ignore[arg-type]


def _evaluate(
    dev: Device, *, root: RootInfo, probe: BlankProbe, all_serials: Sequence[str],
    force: bool = False,
) -> list[Check]:
    """evaluate_write_safety with force defaulting to False (the safe, non-forced
    case), so only tests that exercise --force pass it explicitly.
    """
    return evaluate_write_safety(
        dev, root=root, probe=probe, all_serials=all_serials, force=force
    )


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


def test_directly_mounted_whole_disk_is_rejected():
    # A mountpoint on the whole disk itself (no partitions) must be caught too,
    # not just a mounted child - all_mountpoints walks the device itself first.
    dev = _disk(mountpoints=("/mnt/data",))
    check = check_not_mounted(dev)
    assert not check.ok
    assert "/mnt/data" in check.detail


def test_unmounted_disk_accepted():
    [usb] = parse_lsblk(load_text("lsblk_usb_sda.json"))
    assert check_not_mounted(usb).ok


# --- system disk ----------------------------------------------------------

def test_disk_backing_root_is_rejected():
    dev = _disk(name="nvme0n1", path="/dev/nvme0n1")
    root = _root(source="/dev/nvme0n1p4", parent_disks=("nvme0n1p4", "nvme0n1"))
    assert not check_not_system_disk(dev, root).ok


def test_system_disk_matched_by_path_when_name_differs():
    # The name doesn't match a parent disk, but the /dev path does - still refuse.
    dev = _disk(name="something-else", path="/dev/nvme0n1")
    root = _root(source="/dev/nvme0n1p4", parent_disks=("nvme0n1",))
    assert not check_not_system_disk(dev, root).ok


def test_system_disk_matched_when_parent_reported_with_dev_prefix():
    # The root walk may report a parent as a full "/dev/..." path; the guard must
    # still match it against the target rather than depend on the bare-name form.
    dev = _disk(name="sda", path="/dev/sda")
    root = _root(source="/dev/sda2", parent_disks=("/dev/sda",))
    assert not check_not_system_disk(dev, root).ok


def test_disk_not_backing_root_accepted():
    dev = _disk(name="sda", path="/dev/sda")
    root = _root(source="/dev/nvme0n1p4", parent_disks=("nvme0n1p4", "nvme0n1"))
    assert check_not_system_disk(dev, root).ok


def test_unresolved_root_passes_but_flags_uncertainty():
    dev = _disk(name="sda", path="/dev/sda")
    root = _root(source="zfs/root", resolved=False)
    check = check_not_system_disk(dev, root)
    assert check.ok
    assert "cannot resolve" in check.detail


# --- blank ----------------------------------------------------------------

def test_blank_disk_accepted():
    assert check_blank(_disk(), _probe()).ok


def test_disk_with_signature_rejected():
    probe = _probe(signatures=("ntfs",))
    check = check_blank(_disk(), probe)
    assert not check.ok
    assert "ntfs" in check.detail


def test_blank_probe_error_fails_closed():
    # A probe that errored must be treated as non-blank, never "looks empty".
    probe = _probe(probe_error=True)
    assert not probe.is_blank
    assert not check_blank(_disk(), probe).ok


def test_holders_or_children_reject():
    assert not check_blank(_disk(), _probe(holders=("dm-0",))).ok
    assert not check_blank(_disk(), _probe(children=("sdx1",))).ok


# --- serial uniqueness ----------------------------------------------------

def test_missing_serial_rejected():
    assert not check_serial_unique(_disk(serial=None), []).ok


def test_empty_string_serial_rejected():
    # A cheap USB bridge reporting a blank serial must fail closed, same as None.
    assert not check_serial_unique(_disk(serial=""), []).ok


def test_duplicate_serial_rejected():
    dev = _disk(serial="DUP")
    assert not check_serial_unique(dev, ["DUP", "DUP", "OTHER"]).ok


def test_serial_absent_from_enumeration_fails_closed():
    # Our own disk's serial isn't among the attached disks at all - an enumeration
    # anomaly (the serial list is a separate pass than the target). We can't
    # confirm identity, so fail closed rather than pass as "unique".
    dev = _disk(serial="GHOST")
    check = check_serial_unique(dev, ["OTHER1", "OTHER2"])
    assert not check.ok
    assert "not be found" in check.detail or "not found" in check.detail


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
    checks = _evaluate(
        dev,
        root=_root(source="/dev/nvme0n1p4", parent_disks=("nvme0n1",)),
        probe=_probe(),
        all_serials=["UNIQUE"],
    )
    assert blocking_failures(checks) == []


def test_force_downgrades_only_blank():
    dev = _disk(serial="UNIQUE", type="part")  # whole-disk fails
    checks = _evaluate(
        dev,
        # An established root that isn't this device: force may downgrade blank.
        root=_root(source="/dev/nvme0n1p4", parent_disks=("nvme0n1",)),
        probe=_probe(signatures=("ext4",)),  # blank fails
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
    checks = _evaluate(
        dev,
        root=_root(source="zfs/root", resolved=False),
        probe=_probe(signatures=("ext4",)),  # non-blank
        all_serials=["UNIQUE"],
        force=True,
    )
    failures = {c.name for c in blocking_failures(checks)}
    assert "blank" in failures


def test_force_does_not_downgrade_blank_when_root_walk_empty():
    # resolved=True but no parent disks == not actually established; same risk as
    # an unresolved root, so force must not downgrade blank here either.
    dev = _disk(serial="UNIQUE")
    checks = _evaluate(
        dev,
        root=_root(source="/dev/x", parent_disks=(), resolved=True),
        probe=_probe(signatures=("ext4",)),
        all_serials=["UNIQUE"],
        force=True,
    )
    failures = {c.name for c in blocking_failures(checks)}
    assert "blank" in failures


def test_force_does_not_downgrade_blank_when_probe_errored():
    # A probe that could not run leaves the disk's in-use state unknown. --force
    # overrides data we positively read (signatures/holders), not a state we
    # failed to read, so an errored probe keeps the blank guard blocking.
    dev = _disk(serial="UNIQUE")
    checks = _evaluate(
        dev,
        root=_root(source="/dev/nvme0n1p4", parent_disks=("nvme0n1",)),
        probe=_probe(probe_error=True),
        all_serials=["UNIQUE"],
        force=True,
    )
    failures = {c.name for c in blocking_failures(checks)}
    assert "blank" in failures


def test_force_clears_a_non_blank_but_otherwise_good_disk():
    # The positive case: an established, unrelated root and a real signature (not a
    # probe error). Force downgrades blank and nothing else blocks the write.
    dev = _disk(serial="UNIQUE")
    checks = _evaluate(
        dev,
        root=_root(source="/dev/nvme0n1p4", parent_disks=("nvme0n1",)),
        probe=_probe(signatures=("ext4",)),
        all_serials=["UNIQUE"],
        force=True,
    )
    assert blocking_failures(checks) == []


def test_empty_parent_disks_is_uncertain_not_clean():
    # A resolved root that names no disk cannot clear a target as "not the system
    # disk"; the guard must flag uncertainty rather than pass cleanly.
    dev = _disk(name="sda", path="/dev/sda")
    check = check_not_system_disk(dev, _root(source="/dev/x", parent_disks=(), resolved=True))
    assert check.ok  # non-blocking, but...
    assert "cannot resolve" in check.detail  # ...flagged as uncertain, not "does not back /"


def test_force_does_not_bypass_system_disk_guard():
    dev = _disk(name="nvme0n1", path="/dev/nvme0n1", serial="UNIQUE")
    checks = _evaluate(
        dev,
        root=_root(source="/dev/nvme0n1p4", parent_disks=("nvme0n1",)),
        probe=_probe(signatures=("ext4",)),
        all_serials=["UNIQUE"],
        force=True,
    )
    failures = {c.name for c in blocking_failures(checks)}
    assert "not-system-disk" in failures


def test_force_does_not_bypass_serial_guard():
    # force downgrades only the blank check; a duplicate serial (an ambiguous or
    # wrong node) must keep blocking, as the docstring promises.
    dev = _disk(serial="DUP")
    checks = _evaluate(
        dev,
        root=_root(source="/dev/nvme0n1p4", parent_disks=("nvme0n1",)),
        probe=_probe(signatures=("ext4",)),
        all_serials=["DUP", "DUP"],
        force=True,
    )
    failures = {c.name for c in blocking_failures(checks)}
    assert "unique-serial" in failures


def test_force_does_not_downgrade_blank_when_a_holder_is_present():
    # A live kernel holder (assembled md array / open LUKS / active LVM PV) means
    # the disk is in use right now. The mount guard won't catch an unmounted
    # holder, so - unlike a passive signature - force must NOT wave it past.
    dev = _disk(serial="UNIQUE")
    checks = _evaluate(
        dev,
        root=_root(source="/dev/nvme0n1p4", parent_disks=("nvme0n1",)),
        probe=_probe(holders=("dm-0",)),  # non-blank due to an active holder
        all_serials=["UNIQUE"],
        force=True,
    )
    failures = {c.name for c in blocking_failures(checks)}
    assert "blank" in failures


def test_force_still_downgrades_blank_for_a_passive_signature():
    # The counterpart: a purely passive on-disk signature (no holder, no probe
    # error) is exactly what force is for, so blank is downgraded and clears.
    dev = _disk(serial="UNIQUE")
    checks = _evaluate(
        dev,
        root=_root(source="/dev/nvme0n1p4", parent_disks=("nvme0n1",)),
        probe=_probe(signatures=("ext4",)),
        all_serials=["UNIQUE"],
        force=True,
    )
    assert "blank" not in {c.name for c in blocking_failures(checks)}


def test_force_does_not_bypass_mount_guard():
    # A genuinely mounted disk (real lsblk fixture with children on / and /boot)
    # must stay blocked even under --force.
    [nvme] = parse_lsblk(load_text("lsblk_nvme_system.json"))
    assert nvme.serial is not None
    checks = _evaluate(
        nvme,
        # An established, unrelated root so only the mount guard is at issue.
        root=_root(source="/dev/other", parent_disks=("other",)),
        probe=_probe(signatures=("ext4",)),
        all_serials=[nvme.serial],
        force=True,
    )
    failures = {c.name for c in blocking_failures(checks)}
    assert "not-mounted" in failures


def test_check_is_an_immutable_value_object():
    c = Check("x", True, "d")
    assert c == Check("x", True, "d")  # compares by value
    with pytest.raises(FrozenInstanceError):
        c.ok = False  # type: ignore[misc]  # frozen: assignment must raise
