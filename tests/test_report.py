"""Tests for SMART diffing, verdict classification and formatting."""

from __future__ import annotations

import io
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
from drivetest.units import GIB, MIB

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


def test_pending_sectors_worsening_flagged():
    before = replace(HEALTHY, pending_sectors=0)
    after = replace(HEALTHY, pending_sectors=3)
    assert diff_smart(before, after)[0].field == "pending_sectors"


def test_uncorrectable_errors_worsening_flagged():
    before = replace(HEALTHY, uncorrectable_errors=0)
    after = replace(HEALTHY, uncorrectable_errors=2)
    assert diff_smart(before, after)[0].field == "uncorrectable_errors"


def test_missing_counter_is_not_a_change():
    before = replace(HEALTHY, media_errors=None)
    after = replace(HEALTHY, media_errors=2)
    assert diff_smart(before, after) == []  # can't compare a missing baseline


def test_counter_decrease_is_not_a_change():
    # Only increases count as worsening; a lower reading (e.g. a reset) is ignored.
    before = replace(HEALTHY, media_errors=5)
    after = replace(HEALTHY, media_errors=2)
    assert diff_smart(before, after) == []


def test_multiple_counters_worsening_are_all_reported():
    before = replace(HEALTHY, media_errors=0, crc_errors=0)
    after = replace(HEALTHY, media_errors=1, crc_errors=2)
    assert {d.field for d in diff_smart(before, after)} == {"media_errors", "crc_errors"}


def test_changed_verdict_from_real_parsed_reports():
    before = parse_smart_json(load_json("smart_nvme.json"))
    after_obj = load_json("smart_nvme.json")
    after_obj["nvme_smart_health_information_log"]["media_errors"] = 4
    after = parse_smart_json(after_obj)
    deltas = diff_smart(before, after)
    assert deltas and deltas[0].field == "media_errors"
    assert classify_smart(after, deltas) is SmartVerdict.CHANGED


def test_health_self_assessment_flip_is_a_regression():
    # SMART overall-health going PASSED -> FAILED is not a counter, but it is a
    # regression and must make the run CHANGED, not CLEAN.
    before = replace(HEALTHY, health_passed=True)
    after = replace(HEALTHY, health_passed=False)
    assert diff_smart(before, after) == []  # no counter moved
    regressions = health_regressions(before, after)
    assert regressions and "FAILED" in regressions[0]
    assert classify_smart(after, [], regressions) is SmartVerdict.CHANGED


def test_failed_self_assessment_from_unknown_baseline_is_a_regression():
    # A partial pre-run report (health_passed unknown - e.g. SATA-behind-USB where
    # smart_status was absent) must not let a post-run FAILED assessment read as
    # CLEAN just because there was no True -> False transition to compare against.
    before = replace(HEALTHY, health_passed=None)
    after = replace(HEALTHY, health_passed=False)
    assert diff_smart(before, after) == []  # no counter moved
    regressions = health_regressions(before, after)
    assert regressions and "FAILED" in regressions[0]
    assert classify_smart(after, [], regressions) is SmartVerdict.CHANGED


def test_already_failed_before_run_is_not_a_new_regression():
    # Already FAILED before we started -> no regression to attribute to this run.
    before = replace(HEALTHY, health_passed=False)
    after = replace(HEALTHY, health_passed=False)
    assert health_regressions(before, after) == []


def test_nvme_critical_warning_raised_is_a_regression():
    before = replace(HEALTHY, critical_warning=0)
    after = replace(HEALTHY, critical_warning=4)
    regressions = health_regressions(before, after)
    assert regressions and "critical warning" in regressions[0]
    assert classify_smart(after, [], regressions) is SmartVerdict.CHANGED


def test_critical_warning_raised_from_unknown_baseline_is_a_regression():
    # An unknown baseline (None) with a post-run warning is still a regression -
    # the `before.critical_warning or 0` default must treat None as 0, not skip it.
    before = replace(HEALTHY, critical_warning=None)
    after = replace(HEALTHY, critical_warning=4)
    regressions = health_regressions(before, after)
    assert regressions and "critical warning" in regressions[0]


def test_critical_warning_cleared_is_not_a_regression():
    # A warning going away (4 -> 0) is not a new problem introduced by this run.
    before = replace(HEALTHY, critical_warning=4)
    after = replace(HEALTHY, critical_warning=0)
    assert health_regressions(before, after) == []


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
    assert VerifyOutcome(VerifyStatus.FAIL).describe() == "FAIL"
    assert VerifyOutcome(VerifyStatus.OVERHEAT).describe() == "OVERHEAT"
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
    # a sub-GiB non-zero span keeps a decimal instead of collapsing to "0GiB"
    assert format_gib(512 * MIB) == "0.5GiB"
    assert format_gib(GIB + GIB // 2) == "1.5GiB"


def test_format_gib_tiny_nonzero_is_not_shown_as_zero():
    # A region far below 0.1 GiB (small device, many --parts) would round to
    # "0.0GiB" at one decimal; it must read as "<0.1GiB", never as zero.
    assert format_gib(1 * MIB) == "<0.1GiB"
    assert format_gib(40 * MIB) == "<0.1GiB"
    # just above the threshold still renders a real decimal, not "<0.1GiB"
    assert format_gib(80 * MIB) == "0.1GiB"


def test_logger_tees_to_file(tmp_path, capsys):
    summary = tmp_path / "summary.log"
    logger = Logger(summary)
    logger.log("hello")
    logger.log("world")
    assert summary.read_text() == "hello\nworld\n"
    assert capsys.readouterr().out == "hello\nworld\n"


def test_logger_tees_to_explicit_stream(tmp_path):
    # With an explicit stream (the branch the orchestrator uses), output goes to
    # that stream and the file, not to stdout.
    summary = tmp_path / "summary.log"
    stream = io.StringIO()
    logger = Logger(summary, stream=stream)
    logger.log("hello")
    logger.log("world")
    assert summary.read_text() == "hello\nworld\n"
    assert stream.getvalue() == "hello\nworld\n"
