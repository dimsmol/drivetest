"""Tests for SMART JSON parsing and access-mode detection."""

from __future__ import annotations

import json
import subprocess

import pytest

from drivetest.smart import (
    CELSIUS_KELVIN_THRESHOLD,
    KELVIN_OFFSET,
    MAX_PLAUSIBLE_TEMP_C,
    MIN_PLAUSIBLE_TEMP_C,
    TEMP_READ_TIMEOUT_S,
    _kelvin_or_celsius,
    detect_access_mode,
    parse_smart_json,
    read_smart,
    read_temperature,
)

from .conftest import FakeRunner, load_json, load_text

# A subprocess.TimeoutExpired, which the runner translates to ProcTimeout, models
# a temperature read that stalled (a wedged bridge under thermal stress).
_TIMEOUT_ERROR = subprocess.TimeoutExpired("cmd", TEMP_READ_TIMEOUT_S)

# A plausible Celsius drive temperature; the Kelvin-conversion tests build their
# input from it with KELVIN_OFFSET, so the round-trip is obvious at a glance.
SAMPLE_TEMP_C = 40


def test_parse_nvme_report():
    info = parse_smart_json(load_json("smart_nvme.json"))
    assert info.model == "WD_BLACK SN850X 2000GB"
    assert info.serial == "255106803016"
    assert info.health_passed is True
    assert info.temperature_c == 34
    assert info.media_errors == 0
    assert info.available_spare == 100
    assert info.percentage_used == 0
    assert info.unsafe_shutdowns == 1
    assert info.critical_warning == 0
    assert info.has_report


def test_parse_ata_report_attributes():
    info = parse_smart_json(load_json("smart_ata.json"))
    assert info.model == "Samsung SSD 860 EVO 1TB"
    assert info.health_passed is True
    assert info.temperature_c == 30
    assert info.reallocated_sectors == 0
    assert info.pending_sectors == 0
    assert info.uncorrectable_errors == 0
    assert info.crc_errors == 0
    assert info.power_on_hours == 4200


def test_parse_nvme_failing_report():
    # A genuinely failing NVMe drive must parse as FAILED with its nonzero
    # counters intact - the parser boundary, not just hand-built SmartInfo values.
    info = parse_smart_json(load_json("smart_nvme_failing.json"))
    assert info.has_report
    assert info.health_passed is False
    assert info.critical_warning == 4
    assert info.media_errors == 12
    assert info.available_spare == 5
    assert info.percentage_used == 98


def test_parse_ata_failing_report():
    # A failing SATA drive: FAILED self-assessment and nonzero sector/CRC counters
    # must be extracted from the attribute table (raw values), not read as 0.
    info = parse_smart_json(load_json("smart_ata_failing.json"))
    assert info.health_passed is False
    assert info.reallocated_sectors == 8
    assert info.pending_sectors == 3
    assert info.uncorrectable_errors == 2
    assert info.crc_errors == 15


def test_ata_attr_matches_by_name_when_id_differs():
    # The id-or-name fallback: a row whose id doesn't match but whose name does.
    obj = {
        "model_name": "M",
        "serial_number": "S",
        "ata_smart_attributes": {
            "table": [{"id": 999, "name": "Reallocated_Sector_Ct", "raw": {"value": 7}}]
        },
    }
    assert parse_smart_json(obj).reallocated_sectors == 7


def test_missing_fields_become_none():
    # A bare report (model/serial only) has no counters/health - all None, not 0.
    info = parse_smart_json({"model_name": "M", "serial_number": "S"})
    assert info.has_report
    assert info.health_passed is None  # no smart_status
    assert info.media_errors is None
    assert info.available_spare is None
    assert info.critical_warning is None
    assert info.reallocated_sectors is None  # no ata table


def test_temperature_falls_back_to_nvme_log_when_no_top_level():
    obj = load_json("smart_nvme.json")
    del obj["temperature"]
    info = parse_smart_json(obj)
    assert info.temperature_c == 34  # from nvme_smart_health_information_log


def test_empty_report_has_no_report():
    info = parse_smart_json({"device": {"name": "/dev/sda"}})
    assert not info.has_report


# A minimal smartctl --json -i payload that counts as a real report (has_report
# keys off model/serial), regardless of the exit status smartctl would set.
_REPORT_JSON = '{"model_name": "X", "serial_number": "Y"}'


def test_detect_access_mode_prefers_bare(fake_runner: FakeRunner):
    # Bare/auto yields a real report -> chosen first, no bridge mode tried.
    fake_runner.add("smartctl", contains=["-i"], stdout=_REPORT_JSON)
    assert detect_access_mode(fake_runner, "/dev/sda") == []


def test_detect_access_mode_finds_working_bridge_mode():
    runner = FakeRunner()
    # Only the sntasmedia bridge mode returns a real report; bare/others don't.
    runner.add("smartctl", contains=["-i", "sntasmedia"], stdout=_REPORT_JSON)
    runner.add("smartctl", contains=["-i"], stdout="No such device")
    assert detect_access_mode(runner, "/dev/sda") == ["-d", "sntasmedia"]


def test_detect_access_mode_accepts_report_despite_nonzero_exit():
    # smartctl sets diagnostic bits (non-zero exit) on an aging drive but still
    # prints a full report - the correct mode must not be skipped.
    runner = FakeRunner()
    runner.add("smartctl", contains=["-i"], stdout=_REPORT_JSON, returncode=4)
    assert detect_access_mode(runner, "/dev/sda") == []


def test_detect_access_mode_falls_back_when_nothing_works():
    runner = FakeRunner()
    runner.add("smartctl", contains=["-i"], stdout="No such device")
    assert detect_access_mode(runner, "/dev/sda") == []


def test_read_smart_returns_no_report_on_bad_json(fake_runner: FakeRunner):
    fake_runner.add("smartctl", contains=["--json"], stdout="No such device", returncode=2)
    info = read_smart(fake_runner, "/dev/sda", [])
    assert not info.has_report


def test_read_smart_parses_valid_json(fake_runner: FakeRunner):
    fake_runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    info = read_smart(fake_runner, "/dev/sda", ["-d", "sntasmedia"])
    assert info.serial == "255106803016"


def test_read_smart_fails_closed_on_non_object_json(fake_runner: FakeRunner):
    # Valid JSON that isn't an object (null/list/number) parses fine but has no
    # .get; it must degrade to a no-report SmartInfo, not raise from the parser.
    fake_runner.add("smartctl", contains=["--json"], stdout="null")
    info = read_smart(fake_runner, "/dev/sda", [])
    assert not info.has_report


def test_detect_access_mode_skips_non_object_json(fake_runner: FakeRunner):
    # A bridge mode that returns non-object JSON must be skipped (no report), not
    # crash on parse; with no mode yielding a report, detection falls back to bare.
    fake_runner.add("smartctl", contains=["-i"], stdout="[]")
    assert detect_access_mode(fake_runner, "/dev/sda") == []


def test_read_temperature_smartctl_path_rejects_out_of_range(fake_runner: FakeRunner):
    # The plausibility window guards the smartctl (non-NVMe) branch too, not just
    # the nvme/Kelvin path: a garbage bridge temperature is dropped to None.
    obj = {"model_name": "M", "serial_number": "S",
           "temperature": {"current": MAX_PLAUSIBLE_TEMP_C + 50}}
    fake_runner.add("smartctl", contains=["--json"], stdout=json.dumps(obj))
    assert read_temperature(fake_runner, "/dev/sda", []) is None


def test_int_field_rejects_json_bool():
    # A JSON boolean must not be coerced to 0/1: a stray `media_errors: true` in a
    # malformed report reads as unknown (None), never as a real count.
    info = parse_smart_json(
        {"model_name": "M", "nvme_smart_health_information_log": {"media_errors": True}}
    )
    assert info.media_errors is None


def test_health_passed_requires_a_real_bool():
    # Only a real JSON boolean is a verdict; a string "passed" is not a PASS.
    info = parse_smart_json({"model_name": "M", "smart_status": {"passed": "true"}})
    assert info.health_passed is None


def test_read_temperature_nvme_json_kelvin(fake_runner: FakeRunner):
    # nvme-cli reports Kelvin; KELVIN_OFFSET converts it back to Celsius.
    kelvin = SAMPLE_TEMP_C + KELVIN_OFFSET
    fake_runner.add("nvme", contains=["smart-log"], stdout=f'{{"temperature": {kelvin}}}')
    assert read_temperature(fake_runner, "/dev/nvme0n1", []) == SAMPLE_TEMP_C


def test_read_temperature_rejects_out_of_range(fake_runner: FakeRunner):
    # An nvme reading above the plausibility ceiling is rejected; with no usable
    # smartctl fallback either, the result is None.
    too_hot_k = MAX_PLAUSIBLE_TEMP_C + KELVIN_OFFSET + 50
    fake_runner.add("nvme", contains=["smart-log"], stdout=f'{{"temperature": {too_hot_k}}}')
    fake_runner.add("smartctl", contains=["--json"], stdout='{"model_name": "M"}')
    assert read_temperature(fake_runner, "/dev/nvme0n1", []) is None


def test_read_temperature_implausible_nvme_falls_back_to_smartctl(fake_runner: FakeRunner):
    # An implausible nvme reading must not suppress the smartctl fallback: a garbage
    # bridge value from nvme still lets a good smartctl reading through.
    too_hot_k = MAX_PLAUSIBLE_TEMP_C + KELVIN_OFFSET + 50
    fake_runner.add("nvme", contains=["smart-log"], stdout=f'{{"temperature": {too_hot_k}}}')
    fake_runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    assert read_temperature(fake_runner, "/dev/nvme0n1", []) == 34


def test_read_temperature_nvme_non_object_json_falls_back(fake_runner: FakeRunner):
    # nvme exits 0 but prints valid-but-non-object JSON (null): .get would raise, so
    # the nvme branch must fail closed to the smartctl fallback, not propagate.
    fake_runner.add("nvme", contains=["smart-log"], stdout="null")
    fake_runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    assert read_temperature(fake_runner, "/dev/nvme0n1", []) == 34


@pytest.mark.parametrize(
    ("celsius", "accepted"),
    [
        (MIN_PLAUSIBLE_TEMP_C - 1, False),
        (MIN_PLAUSIBLE_TEMP_C, True),
        (MAX_PLAUSIBLE_TEMP_C, True),
        (MAX_PLAUSIBLE_TEMP_C + 1, False),
    ],
)
def test_read_temperature_plausibility_bounds(fake_runner: FakeRunner, celsius, accepted):
    kelvin = celsius + KELVIN_OFFSET
    fake_runner.add("nvme", contains=["smart-log"], stdout=f'{{"temperature": {kelvin}}}')
    # A rejected nvme reading falls back to smartctl; register a report with no
    # temperature so the fallback also yields None and the bound is what's tested.
    fake_runner.add("smartctl", contains=["--json"], stdout='{"model_name": "M"}')
    result = read_temperature(fake_runner, "/dev/nvme0n1", [])
    assert result == (celsius if accepted else None)


def test_read_temperature_falls_back_to_smartctl_for_non_nvme(fake_runner: FakeRunner):
    # A non-NVMe node makes no nvme call; the temperature comes from smartctl.
    fake_runner.add("smartctl", contains=["--json"], stdout=load_text("smart_ata.json"))
    assert read_temperature(fake_runner, "/dev/sda", []) == 30


def test_read_temperature_nvme_falls_back_to_smartctl_on_nvme_failure(fake_runner: FakeRunner):
    fake_runner.add("nvme", contains=["smart-log"], returncode=1)
    fake_runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    assert read_temperature(fake_runner, "/dev/nvme0n1", []) == 34


def test_read_temperature_ignores_nvme_substring_in_non_nvme_path(fake_runner: FakeRunner):
    # A non-NVMe node whose path merely contains "nvme" (e.g. an enclosure dir)
    # must not trigger an `nvme` call. Only a smartctl rule is registered, so a
    # stray nvme call would raise from FakeRunner and fail the test.
    fake_runner.add("smartctl", contains=["--json"], stdout=load_text("smart_ata.json"))
    assert read_temperature(fake_runner, "/dev/nvme-enclosure/sdb", []) == 30


def test_read_temperature_tolerates_missing_nvme_binary(fake_runner: FakeRunner):
    # If the nvme binary is absent (ToolUnavailable), fall back to smartctl rather
    # than let the error propagate out of a best-effort temperature read.
    fake_runner.add("nvme", contains=["smart-log"], error=FileNotFoundError("nvme"))
    fake_runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    assert read_temperature(fake_runner, "/dev/nvme0n1", []) == 34


def test_read_temperature_bounds_both_reads_with_a_timeout(fake_runner: FakeRunner):
    # The live write monitor calls this on every poll and can only abort fio at the
    # ceiling *between* reads, so a read must never block. Both the nvme read and
    # the smartctl fallback must carry TEMP_READ_TIMEOUT_S so a wedged tool can't
    # freeze the ceiling check.
    fake_runner.add("nvme", contains=["smart-log"], stdout="null")  # forces the fallback
    fake_runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    read_temperature(fake_runner, "/dev/nvme0n1", [])
    timeouts = {call.argv[0]: call.timeout for call in fake_runner.calls}
    assert timeouts["nvme"] == TEMP_READ_TIMEOUT_S
    assert timeouts["smartctl"] == TEMP_READ_TIMEOUT_S


def test_read_temperature_nvme_timeout_falls_back_to_smartctl(fake_runner: FakeRunner):
    # A stalled nvme read (ProcTimeout) must not propagate out of a best-effort
    # temperature read; fall through to the smartctl fallback like any other nvme
    # failure, so the monitor loop keeps running.
    fake_runner.add("nvme", contains=["smart-log"], error=_TIMEOUT_ERROR)
    fake_runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    assert read_temperature(fake_runner, "/dev/nvme0n1", []) == 34


def test_read_temperature_returns_none_when_both_reads_time_out(fake_runner: FakeRunner):
    # If both the nvme read and the smartctl fallback stall, the temperature is
    # unknown (None) - which the pacing loops treat as "proceed" - never a hang.
    fake_runner.add("nvme", contains=["smart-log"], error=_TIMEOUT_ERROR)
    fake_runner.add("smartctl", contains=["--json"], error=_TIMEOUT_ERROR)
    assert read_temperature(fake_runner, "/dev/nvme0n1", []) is None


def test_read_smart_fails_closed_on_timeout(fake_runner: FakeRunner):
    # A bounded read that stalls fails closed to a no-report SmartInfo rather than
    # letting ProcTimeout escape the parser's guard.
    fake_runner.add("smartctl", contains=["--json"], error=_TIMEOUT_ERROR)
    info = read_smart(fake_runner, "/dev/sda", [], timeout=TEMP_READ_TIMEOUT_S)
    assert not info.has_report


def test_read_smart_snapshot_is_unbounded_by_default(fake_runner: FakeRunner):
    # The baseline/after snapshots pass no timeout (unbounded), unchanged from
    # before: only the repeated monitor reads need the bound.
    fake_runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    read_smart(fake_runner, "/dev/sda", [])
    assert fake_runner.calls[-1].timeout is None


def test_kelvin_or_celsius():
    assert _kelvin_or_celsius(SAMPLE_TEMP_C + KELVIN_OFFSET) == SAMPLE_TEMP_C  # kelvin in
    assert _kelvin_or_celsius(SAMPLE_TEMP_C) == SAMPLE_TEMP_C                   # already celsius
    assert _kelvin_or_celsius(None) is None


def test_kelvin_threshold_boundary():
    # At the threshold the value is read as Celsius; one above is read as Kelvin.
    assert _kelvin_or_celsius(CELSIUS_KELVIN_THRESHOLD) == CELSIUS_KELVIN_THRESHOLD
    assert (
        _kelvin_or_celsius(CELSIUS_KELVIN_THRESHOLD + 1)
        == CELSIUS_KELVIN_THRESHOLD + 1 - KELVIN_OFFSET
    )
