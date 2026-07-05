"""Logging, SMART comparison and final result classification.

SMART health is compared *structurally* (field by field on the counters that
matter) instead of by diffing scrubbed text - more robust and directly
testable. A logger tees to a summary file so the on-disk record matches what the
user saw on screen.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TextIO

from .smart import HEALTH_COUNTERS, SmartInfo
from .units import GIB


class SmartVerdict(Enum):
    """Semantic verdict of the before/after SMART comparison.

    Member values are stable identifiers; :func:`describe_verdict` renders the
    display text, kept out of the data so wording can change independently.
    """

    CLEAN = "clean"
    CHANGED = "changed"
    UNKNOWN = "unknown"


_SMART_VERDICT_TEXT = {
    SmartVerdict.CLEAN: "clean",
    SmartVerdict.CHANGED: "CHANGED - health worsened",
    SmartVerdict.UNKNOWN: "unknown (post-run SMART read failed - device may have dropped)",
}


def describe_verdict(verdict: SmartVerdict) -> str:
    return _SMART_VERDICT_TEXT[verdict]


class VerifyStatus(Enum):
    """Outcome of the write+verify phase.

    ``SKIPPED`` is a read-only run; the rest correspond to fio's ``RegionResult``
    (the orchestrator maps between them explicitly). Member values are semantic
    identifiers; display text is rendered by :func:`VerifyOutcome.describe`.
    """

    SKIPPED = "skipped"
    PASS = "pass"
    FAIL = "fail"
    OVERHEAT = "overheat"


_VERIFY_STATUS_TEXT = {
    VerifyStatus.SKIPPED: "skipped",
    VerifyStatus.PASS: "PASS",
    VerifyStatus.FAIL: "FAIL",
    VerifyStatus.OVERHEAT: "OVERHEAT",
}


@dataclass(frozen=True)
class VerifyOutcome:
    """The write+verify result as data, not a formatted string.

    ``detail`` is present only for a ``--only`` subset pass (the whole drive is
    verified only once every part has passed across runs); its presence *is* the
    "partial" flag, so the two can't disagree.
    """

    status: VerifyStatus
    detail: str | None = None

    @property
    def needs_attention(self) -> bool:
        return self.status in (VerifyStatus.FAIL, VerifyStatus.OVERHEAT)

    def describe(self) -> str:
        text = _VERIFY_STATUS_TEXT[self.status]
        if self.detail:
            return f"{text} ({self.detail} - not the whole drive)"
        return text


@dataclass(frozen=True)
class SmartDelta:
    """A worsened counter between the before/after SMART snapshots."""

    field: str
    before: int
    after: int


def diff_smart(before: SmartInfo, after: SmartInfo) -> list[SmartDelta]:
    """Return counters that increased (worsened) from ``before`` to ``after``.

    Only error/wear counters are compared; a missing value on either side is
    skipped rather than treated as a change. Non-counter health signals (the
    self-assessment flag, NVMe critical warning) are handled by
    :func:`health_regressions`.
    """
    deltas: list[SmartDelta] = []
    for name, get in HEALTH_COUNTERS:
        b = get(before)
        a = get(after)
        if b is None or a is None:
            continue
        if a > b:
            deltas.append(SmartDelta(field=name, before=b, after=a))
    return deltas


def health_regressions(before: SmartInfo, after: SmartInfo) -> list[str]:
    """Health signals that worsened but are not monotonic wear counters.

    A change here is at least as serious as a counter increase, so it must feed
    the verdict too: the drive's own SMART self-assessment flipping to FAILED, or
    an NVMe critical-warning flag being raised during the run. Kept out of
    :func:`diff_smart` because these are a boolean and a bitmask, not counters.
    """
    reasons: list[str] = []
    if after.health_passed is False and before.health_passed is not False:
        # A currently-FAILED self-assessment must feed the verdict even when the
        # baseline is unknown (None) - e.g. a partial report or SATA-behind-USB
        # where the pre-run report lacked smart_status. Only skip it if the drive
        # was already FAILED before the run (no regression to report).
        if before.health_passed is True:
            reasons.append("SMART self-assessment flipped PASSED -> FAILED")
        else:
            reasons.append("SMART self-assessment reports FAILED")
    before_cw = before.critical_warning or 0
    after_cw = after.critical_warning
    if after_cw is not None:
        # Report only bits that were newly *set* during the run. Comparing the raw
        # value would also flag a pure clear (e.g. 0x07 -> 0x03) as "raised", which
        # is wrong: a warning bitmask that only dropped bits improved, not worsened.
        newly_set = after_cw & ~before_cw
        if newly_set:
            reasons.append(f"NVMe critical warning raised (0x{newly_set:02x})")
    return reasons


def classify_smart(
    after: SmartInfo, deltas: list[SmartDelta], regressions: Sequence[str]
) -> SmartVerdict:
    """Turn the after-snapshot and diff into a verdict.

    A post-run report that isn't a real report (device dropped) is UNKNOWN, not
    clean - an error payload must never be reported as a healthy result. Any
    counter delta or non-counter health regression means CHANGED. A report that
    identifies the device but carries *no* health signal at all (a flaky bridge
    that returned identity only) is UNKNOWN too, not CLEAN - "answered but told us
    nothing" is not a clean bill of health.

    ``regressions`` is required (no default): it is the only channel for
    non-counter regressions such as a raised NVMe critical warning, so a caller
    must consciously supply it rather than silently omit the signal.
    """
    if not after.has_report:
        return SmartVerdict.UNKNOWN
    if deltas or regressions:
        return SmartVerdict.CHANGED
    if not after.has_health_signal:
        return SmartVerdict.UNKNOWN
    return SmartVerdict.CLEAN


class Logger:
    """Tee log lines to stdout and a summary file."""

    def __init__(self, summary_path: Path, *, stream: TextIO | None) -> None:
        self._path = summary_path
        self._stream = stream

    def log(self, message: str) -> None:
        if self._stream is not None:
            print(message, file=self._stream)
        else:
            print(message)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(message + "\n")


def format_gib(num_bytes: int) -> str:
    gib = num_bytes / GIB
    # Whole-GiB and large values render without a fraction (drive-scale sizes);
    # a small non-zero size keeps one decimal so a sub-GiB span (a region on a
    # small device, or with many --parts) never misleadingly prints as "0GiB".
    if gib >= 10 or gib == int(gib):
        return f"{gib:.0f}GiB"
    # A nonzero span below ~0.05 GiB would round to "0.0GiB" at one decimal; show
    # it as "<0.1GiB" so a real region never reads as zero.
    if 0 < gib < 0.05:
        return "<0.1GiB"
    return f"{gib:.1f}GiB"
