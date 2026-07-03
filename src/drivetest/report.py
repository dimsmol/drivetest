"""Logging, SMART comparison and final result classification.

SMART health is compared *structurally* (field by field on the counters that
matter) instead of by diffing scrubbed text - more robust and directly
testable. A logger tees to a summary file so the on-disk record matches what the
user saw on screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from .smart import SmartInfo


class SmartVerdict(StrEnum):
    CLEAN = "clean"
    CHANGED = "CHANGED - health counters worsened"
    UNKNOWN = "unknown (post-run SMART read failed - device may have dropped)"


@dataclass(frozen=True)
class SmartDelta:
    """A worsened counter between the before/after SMART snapshots."""

    field: str
    before: int | None
    after: int | None


def diff_smart(before: SmartInfo, after: SmartInfo) -> list[SmartDelta]:
    """Return counters that increased (worsened) from ``before`` to ``after``.

    Only error/wear counters are compared; a missing value on either side is
    skipped rather than treated as a change.
    """
    deltas: list[SmartDelta] = []
    for field in SmartInfo.HEALTH_COUNTERS:
        b = getattr(before, field)
        a = getattr(after, field)
        if b is None or a is None:
            continue
        if a > b:
            deltas.append(SmartDelta(field=field, before=b, after=a))
    return deltas


def classify_smart(after: SmartInfo, deltas: list[SmartDelta]) -> SmartVerdict:
    """Turn the after-snapshot and diff into a verdict.

    A post-run report that isn't a real report (device dropped) is UNKNOWN, not
    clean - the exact trap the shell version once fell into.
    """
    if not after.has_report:
        return SmartVerdict.UNKNOWN
    if deltas:
        return SmartVerdict.CHANGED
    return SmartVerdict.CLEAN


class Logger:
    """Tee log lines to stdout and a summary file."""

    def __init__(self, summary_path: Path, *, stream: TextIO | None = None) -> None:
        self._path = summary_path
        self._stream = stream

    def log(self, message: str = "") -> None:
        if self._stream is not None:
            print(message, file=self._stream)
        else:
            print(message)
        with open(self._path, "a") as fh:
            fh.write(message + "\n")


def format_gib(num_bytes: int) -> str:
    return f"{num_bytes / (1024 ** 3):.0f}GiB"
