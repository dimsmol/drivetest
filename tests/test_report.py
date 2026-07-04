"""Tests for SMART diffing, verdict classification and formatting."""

from __future__ import annotations

from dataclasses import replace

from drivetest.report import (
    Logger,
    SmartVerdict,
    VerifyOutcome,
    VerifyStatus,
    classify_smart,
    describe_verdict,
    diff_smart,
    format_gib,
    health_regressions,
)
from drivetest.smart import SmartInfo, parse_smart_json
from drivetest.units import GIB

from .conftest import load_json

HEALTHY = SmartInfo(model="M", serial="S", media_errors=0, reallocated_sectors=0)


def test_no_change_is_clean():
    before = HEALTHY
    after = replace(HEALTHY)
    deltas = diff_smart(before, after)
    assert deltas == []
    assert classify_smart(after, deltas) is SmartVerdict.CLEAN


def test_worsened_media_errors_flagged():
    before = HEALTHY
    after = replace(HEALTHY, media_errors=3)
    deltas = diff_smart(before, after)
    assert len(deltas) == 1
    assert deltas[0].field == "media_errors"
    assert deltas[0].before == 0 and deltas[0].after == 3
    assert classify_smart(after, deltas) is SmartVerdict.CHANGED


def test_reallocated_sectors_worsening_flagged():
    before = HEALTHY
    after = replace(HEALTHY, reallocated_sectors=5)
    assert diff_smart(before, after)[0].field == "reallocated_sectors"


def test_crc_errors_worsening_flagged():
    # UDMA CRC increase = flaky USB bridge/cable during an enclosure test.
    before = replace(HEALTHY, crc_errors=0)
    after = replace(HEALTHY, crc_errors=7)
    deltas = diff_smart(before, after)
    assert deltas[0].field == "crc_errors"
    assert classify_smart(after, deltas) is SmartVerdict.CHANGED


def test_missing_counter_is_not_a_change():
    before = replace(HEALTHY, media_errors=None)
    after = replace(HEALTHY, media_errors=2)
    assert diff_smart(before, after) == []  # can't compare a missing baseline


def test_health_self_assessment_flip_is_a_regression():
    # SMART overall-health going PASSED -> FAILED is not a counter, but it is a
    # regression and must make the run CHANGED, not CLEAN.
    before = replace(HEALTHY, health_passed=True)
    after = replace(HEALTHY, health_passed=False)
    assert diff_smart(before, after) == []  # no counter moved
    regressions = health_regressions(before, after)
    assert regressions and "FAILED" in regressions[0]
    assert classify_smart(after, [], regressions) is SmartVerdict.CHANGED


def test_nvme_critical_warning_raised_is_a_regression():
    before = replace(HEALTHY, critical_warning=0)
    after = replace(HEALTHY, critical_warning=4)
    regressions = health_regressions(before, after)
    assert regressions and "critical warning" in regressions[0]
    assert classify_smart(after, [], regressions) is SmartVerdict.CHANGED


def test_stable_health_flags_are_not_a_regression():
    # Health still PASSED and a pre-existing (unchanged) critical warning are not
    # new regressions introduced by this run.
    before = replace(HEALTHY, health_passed=True, critical_warning=2)
    after = replace(HEALTHY, health_passed=True, critical_warning=2)
    assert health_regressions(before, after) == []


def test_absent_report_is_unknown_not_clean():
    # The classic trap: a dropped device's error payload must not read as clean.
    dropped = SmartInfo(raw=None)
    assert not dropped.has_report
    assert classify_smart(dropped, []) is SmartVerdict.UNKNOWN


def test_real_report_stays_clean_when_unchanged():
    info = parse_smart_json(load_json("smart_nvme.json"))
    assert classify_smart(info, []) is SmartVerdict.CLEAN


def test_verify_outcome_needs_attention():
    assert not VerifyOutcome(VerifyStatus.PASS).needs_attention
    assert not VerifyOutcome(VerifyStatus.SKIPPED).needs_attention
    assert VerifyOutcome(VerifyStatus.FAIL).needs_attention
    assert VerifyOutcome(VerifyStatus.OVERHEAT).needs_attention


def test_verify_outcome_describe():
    assert VerifyOutcome(VerifyStatus.PASS).describe() == "PASS"
    assert VerifyOutcome(VerifyStatus.SKIPPED).describe() == "skipped"
    partial = VerifyOutcome(VerifyStatus.PASS, detail="parts 1-4 of 8")
    assert partial.describe() == "PASS (parts 1-4 of 8 - not the whole drive)"


def test_describe_verdict_renders_display_text():
    # Display text is decoupled from the semantic enum member.
    assert describe_verdict(SmartVerdict.CLEAN) == "clean"
    assert "worsened" in describe_verdict(SmartVerdict.CHANGED)
    assert "device may have dropped" in describe_verdict(SmartVerdict.UNKNOWN)


def test_format_gib():
    assert format_gib(50 * GIB) == "50GiB"
    assert format_gib(0) == "0GiB"


def test_logger_tees_to_file(tmp_path, capsys):
    summary = tmp_path / "summary.log"
    logger = Logger(summary)
    logger.log("hello")
    logger.log("world")
    assert summary.read_text() == "hello\nworld\n"
    assert capsys.readouterr().out == "hello\nworld\n"
