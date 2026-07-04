"""Data-safety guards for the destructive write.

These decide whether ``--write`` may touch a device. Every check is a *pure*
function over already-gathered data, so the exact logic that protects unrelated
disks is unit-tested against fixtures - no hardware, no mocking of side effects.

Design principle: **fail closed**. If a fact cannot be positively established
(an lsblk error, an unreadable probe), the safe answer is "not safe to write",
never "looks fine". The IO layer enforces this by raising when a probe fails and
by setting :attr:`BlankProbe.probe_error`.

The guards, and what each defends against:

- whole-disk        - a partition/dm/LVM node was given instead of a disk.
- not-mounted       - the disk (or a child) is in use; also fails closed.
- not-system-disk   - the disk backs ``/`` (through LVM/RAID/LUKS/btrfs).
- blank             - the disk has data/signatures (probably the wrong disk).
- unique-serial     - the serial can't uniquely re-identify the disk pre-write.
- identity-stable   - the node was reassigned to a different disk since confirm.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .devices import Device


@dataclass(frozen=True)
class Check:
    """The outcome of one guard. ``ok=False`` blocks a destructive write."""

    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class BlankProbe:
    """What the blank-check gathered about a disk's contents.

    ``probe_error`` is set when any probe (wipefs, holders, children) could not
    be read; per fail-closed policy that alone marks the disk non-blank.
    """

    holders: tuple[str, ...] = ()
    signatures: tuple[str, ...] = ()
    children: tuple[str, ...] = ()
    probe_error: bool = False

    @property
    def is_blank(self) -> bool:
        return not (self.probe_error or self.holders or self.signatures or self.children)


@dataclass(frozen=True)
class RootInfo:
    """The disk(s) backing ``/``.

    ``parent_disks`` are the physical disks under the root source (walked
    through any LVM/RAID/LUKS layers). ``resolved`` is False when the root
    source is not a plain block device (ZFS, overlay, network root), in which
    case we cannot compare and must warn instead of trust.
    """

    source: str | None
    parent_disks: tuple[str, ...] = ()
    resolved: bool = True


def check_whole_disk(dev: Device) -> Check:
    """The target must be a whole disk, not a partition or dm/LVM/loop node."""
    if dev.is_disk:
        return Check("whole-disk", True, f"{dev.path} is a whole disk")
    return Check(
        "whole-disk",
        False,
        f"{dev.path} is type '{dev.type}', not a whole disk (looks like a partition)",
    )


def check_not_mounted(dev: Device) -> Check:
    """Refuse if the disk or any child is mounted.

    Note the fail-closed half lives in the IO layer: if lsblk cannot be read the
    :class:`Device` is never built and the caller aborts. Here we only inspect a
    device we *did* read.
    """
    mounts = dev.all_mountpoints
    if mounts:
        return Check("not-mounted", False, f"{dev.path} is mounted at: {', '.join(mounts)}")
    return Check("not-mounted", True, f"{dev.path} is not mounted")


def _root_established(root: RootInfo) -> bool:
    """True only if the disk(s) backing ``/`` were positively identified.

    ``resolved`` with an empty ``parent_disks`` is *not* established: the walk
    returned no physical disk, so there is nothing to compare against and the
    target cannot be cleared as "not the system disk". Treated as uncertain.
    """
    return root.resolved and bool(root.parent_disks)


def check_not_system_disk(dev: Device, root: RootInfo) -> Check:
    """Refuse if ``dev`` backs the running system (``/``).

    If the disk backing ``/`` could not be positively established, this returns a
    *passing* check whose detail flags the uncertainty - but that pass is only
    safe while the blank-disk guard still blocks, so :func:`evaluate_write_safety`
    refuses to let ``--force`` downgrade the blank guard in that case.
    """
    if not _root_established(root):
        return Check(
            "not-system-disk",
            True,
            f"cannot resolve the disk backing / (root source: {root.source}); "
            "relying on the blank-disk guard instead",
        )
    if dev.name in root.parent_disks or dev.path in {f"/dev/{p}" for p in root.parent_disks}:
        return Check("not-system-disk", False, f"{dev.path} backs the running system (/)")
    return Check("not-system-disk", True, f"{dev.path} does not back /")


def check_blank(dev: Device, probe: BlankProbe) -> Check:
    """Refuse a non-blank disk (has a signature/partition/holder), fail-closed.

    A brand-new drive is blank; anything found strongly suggests the wrong disk.
    Overridable with ``--force`` by the caller, not here.
    """
    if probe.is_blank:
        return Check("blank", True, f"{dev.path} is blank")
    reasons: list[str] = []
    if probe.probe_error:
        reasons.append("a blank-check probe failed (treating as non-blank)")
    if probe.children:
        reasons.append(f"partitions: {', '.join(probe.children)}")
    if probe.signatures:
        reasons.append(f"signatures: {', '.join(probe.signatures)}")
    if probe.holders:
        reasons.append(f"in use by: {', '.join(probe.holders)}")
    return Check("blank", False, f"{dev.path} is not blank - {'; '.join(reasons)}")


def check_serial_unique(dev: Device, all_serials: Sequence[str]) -> Check:
    """Require a non-empty serial that is unique among attached disks.

    The pre-write identity re-check can only catch a node reassignment if the
    serial uniquely names this disk; cheap USB bridges sometimes report a fixed
    or duplicate serial.
    """
    if not dev.serial:
        return Check(
            "unique-serial", False, f"{dev.path} reports no serial (identity unverifiable)"
        )
    count = sum(1 for s in all_serials if s == dev.serial)
    if count > 1:
        return Check(
            "unique-serial",
            False,
            f"serial '{dev.serial}' is not unique among attached disks ({count} matches)",
        )
    return Check("unique-serial", True, f"serial '{dev.serial}' is unique")


def check_identity_stable(expected: str, current: str) -> Check:
    """Confirm the node still names the same physical device it did earlier."""
    if expected == current:
        return Check("identity-stable", True, "device identity unchanged")
    return Check(
        "identity-stable",
        False,
        f"device identity changed since confirmation: was [{expected}] now [{current}]",
    )


def evaluate_write_safety(
    dev: Device,
    *,
    root: RootInfo,
    probe: BlankProbe,
    all_serials: Sequence[str],
    force: bool = False,
) -> list[Check]:
    """Run every pre-write guard and return their results, in order.

    ``force`` downgrades only the blank check from blocking to advisory, and only
    when the disk backing ``/`` was positively established (so the system-disk
    guard - not the blank guard - is what protects ``/``). When the root disk is
    unresolved, the blank guard is the sole backstop and ``force`` must not
    remove it, or a forced write on e.g. a ZFS/overlay-root system could target
    the live system disk. ``force`` never bypasses the mount, system-disk,
    whole-disk or serial guards.
    """
    checks = [
        check_whole_disk(dev),
        check_not_mounted(dev),
        check_not_system_disk(dev, root),
        check_serial_unique(dev, all_serials),
        check_blank(dev, probe),
    ]
    if force and _root_established(root):
        checks = [
            Check(c.name, True, f"{c.detail} (forced)") if c.name == "blank" and not c.ok else c
            for c in checks
        ]
    return checks


def blocking_failures(checks: Sequence[Check]) -> list[Check]:
    """The subset of checks that block the write."""
    return [c for c in checks if not c.ok]
