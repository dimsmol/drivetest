"""Gather the real-world inputs the pure guards in :mod:`safety` consume.

This is the IO bridge between :mod:`devices` (topology) and :mod:`safety`
(decisions): it runs ``wipefs``/``findmnt``/``lsblk`` and reads ``/sys`` to
build a :class:`~drivetest.safety.BlankProbe` and
:class:`~drivetest.safety.RootInfo`, keeping the guard logic itself pure.
"""

from __future__ import annotations

import os
from typing import Any

from .devices import Device
from .proc import Runner
from .safety import BlankProbe, RootInfo


def gather_blank_probe(runner: Runner, dev: Device, *, sys_block: str = "/sys/block") -> BlankProbe:
    """Probe a disk for any content. Fails closed: any read error sets
    ``probe_error`` so the blank guard treats the disk as non-blank.
    """
    probe_error = False

    dev_sys = os.path.join(sys_block, dev.name)
    try:
        holders = tuple(sorted(os.listdir(os.path.join(dev_sys, "holders"))))
    except FileNotFoundError:
        holders = ()
        # A real whole disk always has a .../holders dir (empty when unused). If
        # it - or the device's /sys entry - is missing, /sys is not in the state
        # we expect: fail closed rather than read the absence as "no holders".
        # (Either the device's /sys entry or the holders/ subdir being absent
        # raises FileNotFoundError; both are unexpected, so both fail closed.)
        probe_error = True
    except OSError:
        holders = ()
        probe_error = True

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
        return RootInfo(source=None, resolved=False)
    try:
        data: dict[str, Any] = result.json()
        filesystems: list[Any] = data.get("filesystems") or []
    # AttributeError/TypeError guard valid-but-non-object JSON; fail closed.
    except (ValueError, AttributeError, TypeError):
        return RootInfo(source=None, resolved=False)
    if not filesystems:
        return RootInfo(source=None, resolved=False)

    first: dict[str, Any] = filesystems[0] or {}
    raw_source = first.get("source")
    if not raw_source:
        return RootInfo(source=None, resolved=False)
    # Strip a btrfs subvolume suffix like "/dev/sda2[/@root]".
    source = str(raw_source).split("[", 1)[0]

    if not source.startswith("/dev/"):
        return RootInfo(source=source, resolved=False)

    walk = runner.run(["lsblk", "-nrso", "NAME", source])
    if not walk.ok:
        # We know the source but can't resolve parents; treat as unresolved.
        return RootInfo(source=source, resolved=False)
    parents = tuple(line.strip() for line in walk.stdout.splitlines() if line.strip())
    # An empty walk (lsblk succeeded but named no disk) leaves nothing to compare
    # the target against, so the root is not actually established - mark it
    # unresolved so the system-disk guard treats it as uncertain, not clean.
    return RootInfo(source=source, parent_disks=parents, resolved=bool(parents))
