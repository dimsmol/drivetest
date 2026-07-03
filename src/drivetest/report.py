"""Logging, SMART comparison and final result classification.

SMART health is compared *structurally* (field by field on the counters that
matter) instead of by diffing scrubbed text - more robust and directly
testable. A logger tees to a summary file so the on-disk record matches what the
user saw on screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TextIO

from .smart import SmartInfo
from .units import GIB


class SmartVerdict(Enum):
    CLEAN = "clean"
    CHANGED = "CHANGED - health counters worsened"
    UNKNOWN = "unknown (post-run SMART read failed - device may have dropped)"


class VerifyStatus(Enum):
    """Outcome of the write+verify phase.

    ``SKIPPED`` is a read-only run; the rest correspond to fio's ``RegionResult``
    (the orchestrator maps between them explicitly). Values are display text.
    """

    SKIPPED = "skipped"
    PASS = "PASS"
    FAIL = "FAIL"
    OVERHEAT = "OVERHEAT"


@dataclass(frozen=True)
class VerifyOutcome:
    """The write+verify result as data, not a formatted string.

    ``partial`` marks a ``--only`` subset pass (the whole drive is verified only
    once every part has passed across runs); ``detail`` carries the human note
    for it, kept out of the control-flow state.
    """

    status: VerifyStatus
    partial: bool = False
    detail: str | None = None

    @property
    def needs_attention(self) -> bool:
        return self.status in (VerifyStatus.FAIL, VerifyStatus.OVERHEAT)

    def describe(self) -> str:
        if self.partial and self.detail:
            return f"{self.status.value} ({self.detail} - not the whole drive)"
        return self.status.value


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
    clean - an error payload must never be reported as a healthy result.
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
    return f"{num_bytes / GIB:.0f}GiB"
