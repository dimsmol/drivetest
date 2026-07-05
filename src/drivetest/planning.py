"""Split a drive into write+verify regions and parse ``--only`` specs.

All pure arithmetic and string parsing.
"""

from __future__ import annotations

from dataclasses import dataclass

from .units import MIB


@dataclass(frozen=True)
class Region:
    """A contiguous ``[offset, offset+size)`` span to write+verify.

    ``index`` is 1-based to match how the user selects parts with ``--only``.
    """

    index: int
    offset: int
    size: int

    @property
    def end(self) -> int:
        return self.offset + self.size


def plan_regions(dev_bytes: int, parts: int) -> list[Region]:
    """Divide ``dev_bytes`` into ``parts`` regions.

    Each non-final region is ``dev_bytes // parts`` rounded *down* to a whole
    MiB (fio's block size), and the final region absorbs the remainder so the
    regions exactly tile the device with no gap or overlap.

    The final region's size may therefore not be a whole MiB. That is fine: both
    ``dev_bytes`` (a device size) and every MiB-aligned offset are multiples of
    the sector size, so the remainder is sector-aligned - fio writes full 1 MiB
    blocks and a final short, still-sector-aligned block, valid under ``--direct``.

    The final region also absorbs the per-region rounding leftover, so it can be
    up to ``parts`` MiB larger than the rest. This is deliberate: it keeps every
    interior boundary MiB-aligned with the simplest exact tiling (matching the
    reference shell implementation). At realistic ``parts`` the imbalance is a few
    MiB on a multi-GiB region - negligible - and it is intentionally not spread
    across regions, to avoid complicating the most resume-critical arithmetic here.
    """
    if parts < 1:
        raise ValueError(f"parts must be >= 1, got {parts}")
    if dev_bytes <= 0:
        raise ValueError(f"dev_bytes must be > 0, got {dev_bytes}")

    part_size = dev_bytes // parts // MIB * MIB
    if parts > 1 and part_size == 0:
        raise ValueError(f"device too small ({dev_bytes} B) to split into {parts} parts")

    regions: list[Region] = []
    for n in range(1, parts + 1):
        offset = (n - 1) * part_size
        size = dev_bytes - offset if n == parts else part_size
        regions.append(Region(index=n, offset=offset, size=size))
    return regions


def quick_region(size_bytes: int) -> Region:
    """A single leading region for a fast ``--quick`` sanity pass."""
    return Region(index=1, offset=0, size=size_bytes)


def parse_only_spec(spec: str, parts: int) -> set[int]:
    """Expand an ``--only`` spec into the set of 1-based part numbers to run.

    Accepts a comma list of ``N``, ``A-B`` (inclusive), or ``A-`` (A..parts):
    ``"4-8"``, ``"5"``, ``"1-3,7"``. Raises ``ValueError`` on a malformed item
    or one outside ``1..parts``.
    """
    if parts < 1:
        raise ValueError(f"parts must be >= 1, got {parts}")

    selected: set[int] = set()
    items = [item.strip() for item in spec.split(",")]
    for item in items:
        if not item:
            raise ValueError(f"empty part in spec '{spec}'")
        a, b = _parse_item(item, parts)
        if not (1 <= a <= b <= parts):
            raise ValueError(f"--only '{item}' out of range 1-{parts}")
        selected.update(range(a, b + 1))
    # Every non-empty item either raised or added at least one part, and there is
    # always at least one item, so ``selected`` is non-empty here.
    return selected


def _is_index(text: str) -> bool:
    """A 1-based part number: ASCII digits only (``str.isdigit`` also accepts
    superscripts and full-width digits, which ``int`` would mishandle).
    """
    return text.isascii() and text.isdigit()


def _parse_item(item: str, parts: int) -> tuple[int, int]:
    """Parse one spec item into an inclusive (a, b) range."""
    if item.endswith("-"):
        head = item[:-1].strip()
        if not _is_index(head):
            raise ValueError(f"bad --only item '{item}' (use N, A-B, or A-)")
        return int(head), parts
    if "-" in item:
        head, _, tail = item.partition("-")
        head, tail = head.strip(), tail.strip()
        if not (_is_index(head) and _is_index(tail)):
            raise ValueError(f"bad --only item '{item}' (use N, A-B, or A-)")
        return int(head), int(tail)
    if not _is_index(item):
        raise ValueError(f"bad --only item '{item}' (use N, A-B, or A-)")
    value = int(item)
    return value, value
