"""Tests for SMART JSON parsing and access-mode detection."""

from __future__ import annotations

from drivetest.smart import (
    KELVIN_OFFSET,
    MAX_PLAUSIBLE_TEMP_C,
    _kelvin_or_celsius,
    detect_access_mode,
    parse_smart_json,
    read_smart,
    read_temperature,
)

from .conftest import FakeRunner, load_json, load_text

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
    assert info.temperature_c == 30
    assert info.reallocated_sectors == 0
    assert info.pending_sectors == 0
    assert info.uncorrectable_errors == 0
    assert info.crc_errors == 0
    assert info.power_on_hours == 4200


def test_temperature_falls_back_to_nvme_log_when_no_top_level():
    obj = load_json("smart_nvme.json")
    del obj["temperature"]
    info = parse_smart_json(obj)
    assert info.temperature_c == 34  # from nvme_smart_health_information_log


def test_empty_report_has_no_report():
    info = parse_smart_json({"device": {"name": "/dev/sda"}})
    assert not info.has_report


def test_detect_access_mode_prefers_bare(fake_runner: FakeRunner):
    fake_runner.add("smartctl", contains=["-i"], returncode=0)
    assert detect_access_mode(fake_runner, "/dev/sda") == []


def test_detect_access_mode_finds_working_bridge_mode():
    runner = FakeRunner()
    # bare and nvme fail; sntasmedia works
    runner.add("smartctl", contains=["-i", "sntasmedia"], returncode=0)
    runner.add("smartctl", contains=["-i"], returncode=2)  # everything else fails
    assert detect_access_mode(runner, "/dev/sda") == ["-d", "sntasmedia"]


def test_read_smart_returns_no_report_on_bad_json(fake_runner: FakeRunner):
    fake_runner.add("smartctl", contains=["--json"], stdout="No such device", returncode=2)
    info = read_smart(fake_runner, "/dev/sda", [])
    assert not info.has_report


def test_read_smart_parses_valid_json(fake_runner: FakeRunner):
    fake_runner.add("smartctl", contains=["--json"], stdout=load_text("smart_nvme.json"))
    info = read_smart(fake_runner, "/dev/sda", ["-d", "sntasmedia"])
    assert info.serial == "255106803016"


def test_read_temperature_nvme_json_kelvin(fake_runner: FakeRunner):
    # nvme-cli reports Kelvin; KELVIN_OFFSET converts it back to Celsius.
    kelvin = SAMPLE_TEMP_C + KELVIN_OFFSET
    fake_runner.add("nvme", contains=["smart-log"], stdout=f'{{"temperature": {kelvin}}}')
    assert read_temperature(fake_runner, "/dev/nvme0n1", []) == SAMPLE_TEMP_C


def test_read_temperature_rejects_out_of_range(fake_runner: FakeRunner):
    # A Kelvin reading that converts to above the plausibility ceiling -> None.
    too_hot_k = MAX_PLAUSIBLE_TEMP_C + KELVIN_OFFSET + 50
    fake_runner.add("nvme", contains=["smart-log"], stdout=f'{{"temperature": {too_hot_k}}}')
    assert read_temperature(fake_runner, "/dev/nvme0n1", []) is None


def test_kelvin_or_celsius():
    assert _kelvin_or_celsius(SAMPLE_TEMP_C + KELVIN_OFFSET) == SAMPLE_TEMP_C  # kelvin in
    assert _kelvin_or_celsius(SAMPLE_TEMP_C) == SAMPLE_TEMP_C                   # already celsius
    assert _kelvin_or_celsius(None) is None
