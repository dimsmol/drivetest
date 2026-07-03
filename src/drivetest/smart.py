"""Read SMART/health/temperature via ``smartctl`` (and ``nvme`` for temp).

``smartctl --json`` (smartmontools >= 7) gives structured health data, so we
address fields by name instead of grepping formatted text, which is brittle
across locales and versions. :func:`parse_smart_json` is pure and tested
against captured NVMe and SATA report fixtures.

USB bridges expose the drive through different passthrough modes; we probe the
common ones and remember the ``-d`` args that work (:func:`detect_access_mode`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .proc import Runner

# smartctl ``-d`` argument sets to try, in order: bare/auto first, then the
# common USB-NVMe bridge modes, then SAT for SATA-behind-USB.
ACCESS_MODES: tuple[tuple[str, ...], ...] = (
    (),
    ("-d", "nvme"),
    ("-d", "sntasmedia"),
    ("-d", "sntrealtek"),
    ("-d", "sat"),
)

# Temperature handling. A reading outside this window is treated as garbage from
# a flaky bridge rather than a real temperature.
MIN_PLAUSIBLE_TEMP_C = 15
MAX_PLAUSIBLE_TEMP_C = 110
KELVIN_OFFSET = 273
# No drive runs this hot in Celsius, so a value above it must be Kelvin.
CELSIUS_KELVIN_THRESHOLD = 200


@dataclass(frozen=True)
class SmartInfo:
    """The health fields we care about, normalized across NVMe and ATA.

    Any field may be ``None`` if the report did not provide it. ``raw`` keeps
    the full parsed JSON for callers that want more.
    """

    model: str | None = None
    serial: str | None = None
    firmware: str | None = None
    health_passed: bool | None = None
    temperature_c: int | None = None
    media_errors: int | None = None
    available_spare: int | None = None
    percentage_used: int | None = None
    unsafe_shutdowns: int | None = None
    critical_warning: int | None = None
    power_on_hours: int | None = None
    reallocated_sectors: int | None = None
    pending_sectors: int | None = None
    uncorrectable_errors: int | None = None
    crc_errors: int | None = None
    raw: dict[str, Any] | None = None

    @property
    def has_report(self) -> bool:
        """True if this looks like a real report (model or serial present).

        Guards against treating an error payload as a clean health result - the
        bug where a disconnected device's "No such device" was reported clean.
        """
        return bool(self.model or self.serial)

    # The counters that, if they worsen across a run, mean trouble. crc_errors
    # (ATA UDMA CRC) flags a flaky cable/bridge - the key signal when testing
    # through a USB enclosure.
    HEALTH_COUNTERS = (
        "media_errors",
        "reallocated_sectors",
        "pending_sectors",
        "uncorrectable_errors",
        "crc_errors",
    )


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ata_attr(obj: dict[str, Any], attr_id: int, name: str) -> int | None:
    """Pull an ATA SMART attribute's raw value by id or (fallback) name."""
    attrs: dict[str, Any] = obj.get("ata_smart_attributes") or {}
    table: list[Any] = attrs.get("table") or []
    for row in table:
        row_obj: dict[str, Any] = row or {}
        if row_obj.get("id") == attr_id or row_obj.get("name") == name:
            raw: dict[str, Any] = row_obj.get("raw") or {}
            return _int(raw.get("value"))
    return None


def parse_smart_json(obj: dict[str, Any]) -> SmartInfo:
    """Parse one ``smartctl --json`` report into a :class:`SmartInfo`.

    Handles both the NVMe health log and the ATA attribute table; picks
    temperature from the top-level ``temperature.current`` when present.
    """
    nvme: dict[str, Any] = obj.get("nvme_smart_health_information_log") or {}
    temperature: dict[str, Any] = obj.get("temperature") or {}
    power_on: dict[str, Any] = obj.get("power_on_time") or {}
    status: dict[str, Any] = obj.get("smart_status") or {}

    temp = _int(temperature.get("current"))
    if temp is None:
        temp = _int(nvme.get("temperature"))

    # NB: use only model_name here - the device's "name" is the /dev path, which
    # is present even in an error payload and would fool ``has_report``.
    return SmartInfo(
        model=obj.get("model_name"),
        serial=obj.get("serial_number"),
        firmware=obj.get("firmware_version"),
        health_passed=status.get("passed"),
        temperature_c=temp,
        media_errors=_int(nvme.get("media_errors")),
        available_spare=_int(nvme.get("available_spare")),
        percentage_used=_int(nvme.get("percentage_used")),
        unsafe_shutdowns=_int(nvme.get("unsafe_shutdowns")),
        critical_warning=_int(nvme.get("critical_warning")),
        power_on_hours=_int(power_on.get("hours")),
        reallocated_sectors=_ata_attr(obj, 5, "Reallocated_Sector_Ct"),
        pending_sectors=_ata_attr(obj, 197, "Current_Pending_Sector"),
        uncorrectable_errors=_ata_attr(obj, 198, "Offline_Uncorrectable"),
        crc_errors=_ata_attr(obj, 199, "UDMA_CRC_Error_Count"),
        raw=obj,
    )


def detect_access_mode(runner: Runner, dev_path: str) -> list[str]:
    """Return the first ``-d`` arg set for which ``smartctl -i`` succeeds.

    Falls back to bare (``[]``) if none clearly work, so the caller can still
    try. Order matters: bare/auto is preferred over an explicit bridge mode.
    """
    for mode in ACCESS_MODES:
        result = runner.run(["smartctl", "-i", *mode, dev_path])
        if result.ok:
            return list(mode)
    return []


def read_smart(runner: Runner, dev_path: str, mode: list[str]) -> SmartInfo:
    """Read a full JSON SMART report. Returns an empty :class:`SmartInfo`
    (``has_report`` False) if the output is not valid JSON - e.g. the device
    dropped and smartctl printed an error instead of a report.
    """
    result = runner.run(["smartctl", "--json", "-x", *mode, dev_path])
    try:
        obj: dict[str, Any] = result.json()
    except ValueError:
        return SmartInfo(raw=None)
    return parse_smart_json(obj)


def read_temperature(runner: Runner, dev_path: str, mode: list[str]) -> int | None:
    """Best-effort current temperature in Celsius.

    Prefers ``nvme smart-log`` (JSON) for an NVMe node, else ``smartctl``. A
    plausibility window (15-110 C) rejects garbage from a flaky bridge.
    """
    temp: int | None = None
    if "nvme" in dev_path:
        result = runner.run(["nvme", "smart-log", dev_path, "-o", "json"])
        if result.ok:
            try:
                payload: dict[str, Any] = result.json()
                temp = _kelvin_or_celsius(payload.get("temperature"))
            except ValueError:
                temp = None
    if temp is None:
        info = read_smart(runner, dev_path, mode)
        temp = info.temperature_c
    if temp is None or not (MIN_PLAUSIBLE_TEMP_C <= temp <= MAX_PLAUSIBLE_TEMP_C):
        return None
    return temp


def _kelvin_or_celsius(value: Any) -> int | None:
    """nvme-cli reports temperature in Kelvin; convert if it looks like it."""
    n = _int(value)
    if n is None:
        return None
    return n - KELVIN_OFFSET if n > CELSIUS_KELVIN_THRESHOLD else n
