"""Block-device enumeration and identity, backed by ``lsblk --json``.

Using lsblk's JSON output (instead of scraping columns) removes a whole class
of parsing bugs: tree glyphs, locale-dependent column widths, and ambiguous
whitespace. :func:`parse_lsblk` is pure, so it is exhaustively unit-tested
against captured real output.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from .proc import Runner, run_json

# The columns we ask lsblk for. ``-b`` makes SIZE an integer byte count.
LSBLK_COLUMNS = "NAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,TRAN,MOUNTPOINTS"


@dataclass(frozen=True)
class Device:
    """A block device (disk or partition) and the children beneath it.

    Fields mirror lsblk. ``size`` is in bytes. ``model``/``serial``/``wwn`` come
    from lsblk, so for a USB-bridged drive they describe the *bridge*, not the
    drive - that is fine for identity/uniqueness purposes (SMART supplies the
    real drive identity separately).
    """

    path: str
    name: str
    type: str
    size: int
    model: str | None = None
    serial: str | None = None
    wwn: str | None = None
    tran: str | None = None
    mountpoints: tuple[str, ...] = ()
    children: tuple[Device, ...] = field(default_factory=tuple)

    @property
    def is_disk(self) -> bool:
        return self.type == "disk"

    @property
    def identity(self) -> str:
        """A stable fingerprint used to detect the node being reassigned to a
        different physical device (e.g. a USB replug) between confirmation and
        write. Mirrors the shell script's ``SERIAL,WWN,SIZE,MODEL``.
        """
        return "|".join(
            "" if v is None else str(v)
            for v in (self.serial, self.wwn, self.size, self.model)
        )

    def walk(self) -> list[Device]:
        """This device followed by every descendant, depth-first."""
        out: list[Device] = [self]
        for child in self.children:
            out.extend(child.walk())
        return out

    @property
    def all_mountpoints(self) -> list[str]:
        """Non-empty mountpoints across this device and all descendants."""
        seen: list[str] = []
        for dev in self.walk():
            seen.extend(mp for mp in dev.mountpoints if mp)
        return seen


def _clean(value: Any) -> str | None:
    """Normalize a stringy lsblk field: strip, map blank/None to None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _device_from_node(node: dict[str, Any]) -> Device:
    raw_mounts: list[Any] = node.get("mountpoints") or []
    mountpoints = tuple(str(mp) for mp in raw_mounts if mp)
    raw_children: list[Any] = node.get("children") or []
    children = tuple(_device_from_node(child) for child in raw_children)
    size = node.get("size")
    return Device(
        path=str(node.get("path") or f"/dev/{node.get('name')}"),
        name=str(node.get("name") or ""),
        type=str(node.get("type") or ""),
        size=int(size) if size is not None else 0,
        model=_clean(node.get("model")),
        serial=_clean(node.get("serial")),
        wwn=_clean(node.get("wwn")),
        tran=_clean(node.get("tran")),
        mountpoints=mountpoints,
        children=children,
    )


def parse_lsblk(data: dict[str, Any] | str) -> list[Device]:
    """Parse ``lsblk -Jb`` output (a dict or JSON string) into devices."""
    parsed: Any = json.loads(data) if isinstance(data, str) else data
    if not isinstance(parsed, dict):
        raise ValueError("lsblk output is not a JSON object")
    obj = cast("dict[str, Any]", parsed)
    nodes: list[Any] = obj.get("blockdevices") or []
    return [_device_from_node(node) for node in nodes]


def canonical_path(path: str) -> str:
    """Resolve a ``/dev/disk/by-id/...`` symlink to the real node, like
    ``readlink -f``. Guards downstream string comparisons against a symlink
    slipping past a path check.
    """
    return os.path.realpath(path)


def list_devices(runner: Runner, *, path: str | None = None) -> list[Device]:
    """Enumerate block devices via lsblk. If ``path`` is given, restrict to it."""
    argv = ["lsblk", "-Jb", "-o", LSBLK_COLUMNS]
    if path is not None:
        argv.append(path)
    data: Any = run_json(runner, argv)
    return parse_lsblk(data)


def find_device(runner: Runner, path: str) -> Device:
    """Return the whole-device model for ``path`` (canonicalized first).

    Raises ``LookupError`` if lsblk reports nothing for the path.
    """
    real = canonical_path(path)
    devices = list_devices(runner, path=real)
    if not devices:
        raise LookupError(f"lsblk returned no device for {real}")
    return devices[0]


def all_serials(devices: Sequence[Device]) -> list[str]:
    """Every non-empty serial among the given top-level disks."""
    return [d.serial for d in devices if d.serial]
