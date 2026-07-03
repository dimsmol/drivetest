"""Tests for SMART diffing, verdict classification and formatting."""

from __future__ import annotations

from dataclasses import replace

from drivetest.report import (
    Logger,
    SmartVerdict,
    classify_smart,
    diff_smart,
    format_gib,
)
from drivetest.smart import SmartInfo

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


def test_absent_report_is_unknown_not_clean():
    # The classic trap: a dropped device's error payload must not read as clean.
    dropped = SmartInfo(raw=None)
    assert not dropped.has_report
    assert classify_smart(dropped, []) is SmartVerdict.UNKNOWN


def test_real_report_stays_clean_when_unchanged():
    from drivetest.smart import parse_smart_json

    info = parse_smart_json(load_json("smart_nvme.json"))
    assert classify_smart(info, []) is SmartVerdict.CLEAN


def test_format_gib():
    assert format_gib(50 * 1024**3) == "50GiB"
    assert format_gib(0) == "0GiB"


def test_logger_tees_to_file(tmp_path, capsys):
    summary = tmp_path / "summary.log"
    logger = Logger(summary)
    logger.log("hello")
    logger.log("world")
    assert summary.read_text() == "hello\nworld\n"
    assert capsys.readouterr().out == "hello\nworld\n"
