"""Gather the real-world inputs the pure guards in :mod:`safety` consume.

This is the IO bridge between :mod:`devices` (topology) and :mod:`safety`
(decisions): it runs ``wipefs``/``findmnt``/``lsblk`` and reads ``/sys`` to
build a :class:`~drivetest.safety.BlankProbe` and
:class:`~drivetest.safety.RootInfo`, keeping the guard logic itself pure.
"""

from __future__ import annotations

import os
from typing import Any, cast

from .devices import Device
from .proc import Runner
from .safety import BlankProbe, RootInfo


def gather_blank_probe(runner: Runner, dev: Device, *, sys_block: str) -> BlankProbe:
    """Probe a disk for any content. Fails closed: any read error sets
    ``probe_error`` so the blank guard treats the disk as non-blank.
    """
    probe_error = False

    # Holders live at /sys/block/<disk>/holders for the whole disk and at
    # /sys/block/<disk>/<part>/holders for each partition. A holder on a
    # *partition* (an assembled md member, an open LUKS mapping, or an active LVM
    # PV on that partition) is exactly what --force must not wave past, yet it does
    # not appear in the whole-disk holders/ - so aggregate across the disk and its
    # partitions, not the disk alone.
    disk_sys = os.path.join(sys_block, dev.name)
    holder_dirs = [os.path.join(disk_sys, "holders")]
    holder_dirs += [
        os.path.join(disk_sys, child.name, "holders")
        for child in dev.children
        if child.type == "part"
    ]
    found_holders: list[str] = []
    for holder_dir in holder_dirs:
        try:
            found_holders.extend(os.listdir(holder_dir))
        except OSError:
            # A real whole disk (and each of its partitions) always has a holders/
            # dir, empty when unused. If it - or the device's /sys entry - is
            # missing or unreadable, /sys is not in the state we expect: fail
            # closed rather than read the absence as "no holders".
            probe_error = True
    holders = tuple(sorted(found_holders))

    signatures: tuple[str, ...] = ()
    result = runner.run(["wipefs", "-n", "-J", dev.path])
    if result.ok:
        try:
            data: dict[str, Any] = result.json()
            sigs: list[Any] = data.get("signatures") or []
            found: list[str] = []
            for sig in sigs:
                sig_obj: dict[str, Any] = sig or {}
                found.append(str(sig_obj.get("type", "?")))
            signatures = tuple(found)
        # AttributeError/TypeError guard valid-but-non-object JSON (null, [], a
        # bare number): .get would raise rather than JSONDecodeError. Fail closed.
        except (ValueError, AttributeError, TypeError):
            probe_error = True
    else:
        probe_error = True

    children = tuple(child.name for child in dev.children)

    return BlankProbe(
        holders=holders,
        signatures=signatures,
        children=children,
        probe_error=probe_error,
    )


def gather_root_info(runner: Runner) -> RootInfo:
    """Determine the physical disk(s) backing ``/``.

    Walks the root source down to its parent disks through any LVM/RAID/LUKS
    layers (``lsblk -s``). If the root source is not a plain block device (ZFS,
    overlay, network root), returns ``resolved=False`` so the system-disk guard
    warns instead of trusting.
    """
    result = runner.run(["findmnt", "-J", "-o", "SOURCE,TARGET", "/"])
    if not result.ok:
        return RootInfo(source=None, parent_disks=(), resolved=False)
    try:
        data: dict[str, Any] = result.json()
        filesystems: Any = data.get("filesystems")
    # AttributeError/TypeError guard valid-but-non-object JSON; fail closed.
    except (ValueError, AttributeError, TypeError):
        return RootInfo(source=None, parent_disks=(), resolved=False)
    # Fail closed on any malformed shape: a non-list "filesystems", an empty list,
    # or a non-object first entry. The nested cases matter as much as the top-level
    # one - reaching into a bad payload here must not crash past the guard above.
    if not isinstance(filesystems, list) or not filesystems:
        return RootInfo(source=None, parent_disks=(), resolved=False)
    if not isinstance(filesystems[0], dict):
        return RootInfo(source=None, parent_disks=(), resolved=False)
    first = cast("dict[str, Any]", filesystems[0])
    raw_source = first.get("source")
    if not raw_source:
        return RootInfo(source=None, parent_disks=(), resolved=False)
    # Strip a btrfs subvolume suffix like "/dev/sda2[/@root]".
    source = str(raw_source).split("[", 1)[0]

    if not source.startswith("/dev/"):
        return RootInfo(source=source, parent_disks=(), resolved=False)

    walk = runner.run(["lsblk", "-nrso", "NAME", source])
    if not walk.ok:
        # We know the source but can't resolve parents; treat as unresolved.
        return RootInfo(source=source, parent_disks=(), resolved=False)
    parents = tuple(line.strip() for line in walk.stdout.splitlines() if line.strip())
    # An empty walk (lsblk succeeded but named no disk) leaves nothing to compare
    # the target against, so the root is not actually established - mark it
    # unresolved so the system-disk guard treats it as uncertain, not clean.
    return RootInfo(source=source, parent_disks=parents, resolved=bool(parents))
