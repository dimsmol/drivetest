"""Read SMART/health/temperature via ``smartctl`` (and ``nvme`` for temp).

``smartctl --json`` (smartmontools >= 7) gives structured health data, so we
address fields by name instead of grepping formatted text, which is brittle
across locales and versions. :func:`parse_smart_json` is pure and tested
against captured NVMe and SATA report fixtures.

USB bridges expose the drive through different passthrough modes; we probe the
common ones and remember the ``-d`` args that work (:func:`detect_access_mode`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .proc import Runner, ToolNotFound
from .tools import is_nvme_target

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
# a flaky bridge rather than a real temperature. The floor is the coldest a drive
# under active test could plausibly be (a cold room), not a physical limit: a
# rejected reading and a genuine low one drive the pacing loops identically (both
# count as "proceed"), so the floor only decides whether a cold reading is shown
# or dropped - it is deliberately low to avoid discarding a legitimately cool drive.
MIN_PLAUSIBLE_TEMP_C = 5
MAX_PLAUSIBLE_TEMP_C = 110
# Rounded from 273.15; the ~0.15 C bias is well under the integer rounding of the
# result, so it makes no practical difference to a whole-degree temperature.
KELVIN_OFFSET = 273
# No drive runs this hot in Celsius, so a value above it must be Kelvin.
CELSIUS_KELVIN_THRESHOLD = 200

# ATA SMART attribute ids (the smartctl ``id`` field) for the health counters we
# read; paired with their canonical names as an id-or-name fallback.
ATA_ATTR_REALLOCATED_SECTORS = 5
ATA_ATTR_PENDING_SECTORS = 197
ATA_ATTR_OFFLINE_UNCORRECTABLE = 198
ATA_ATTR_UDMA_CRC_ERRORS = 199


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


# The counters that, if they worsen across a run, mean trouble. Each pairs the
# field's display name with a typed accessor, so a rename is caught by the type
# checker instead of failing at runtime the way a string ``getattr`` would.
# crc_errors (ATA UDMA CRC) flags a flaky cable/bridge - the key signal when
# testing through a USB enclosure.
HEALTH_COUNTERS: tuple[tuple[str, Callable[[SmartInfo], int | None]], ...] = (
    ("media_errors", lambda i: i.media_errors),
    ("reallocated_sectors", lambda i: i.reallocated_sectors),
    ("pending_sectors", lambda i: i.pending_sectors),
    ("uncorrectable_errors", lambda i: i.uncorrectable_errors),
    ("crc_errors", lambda i: i.crc_errors),
)


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    """Only a real JSON boolean is a verdict; anything else is 'unknown'."""
    return value if isinstance(value, bool) else None


def _ata_attr(obj: dict[str, Any], attr_id: int, name: str) -> int | None:
    """Pull an ATA SMART attribute's raw value by id or (fallback) name."""
    attrs: dict[str, Any] = obj.get("ata_smart_attributes") or {}
    table: list[Any] = attrs.get("table") or []
    for row in table:
        row_obj: dict[str, Any] = row or {}
        # First row matching by id or (fallback) canonical name. The health ids we
        # read (5/197/198/199) are standardized, so a first-match is unambiguous in
        # practice, even though a vendor could in theory reuse an id for another name.
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
        health_passed=_bool_or_none(status.get("passed")),
        temperature_c=temp,
        media_errors=_int(nvme.get("media_errors")),
        # available_spare is kept for display; spare *exhaustion* (spare below the
        # firmware's available_spare_threshold) is already surfaced by the
        # critical_warning bit we read, so the threshold itself isn't parsed.
        available_spare=_int(nvme.get("available_spare")),
        percentage_used=_int(nvme.get("percentage_used")),
        unsafe_shutdowns=_int(nvme.get("unsafe_shutdowns")),
        critical_warning=_int(nvme.get("critical_warning")),
        power_on_hours=_int(power_on.get("hours")),
        reallocated_sectors=_ata_attr(obj, ATA_ATTR_REALLOCATED_SECTORS, "Reallocated_Sector_Ct"),
        pending_sectors=_ata_attr(obj, ATA_ATTR_PENDING_SECTORS, "Current_Pending_Sector"),
        uncorrectable_errors=_ata_attr(
            obj, ATA_ATTR_OFFLINE_UNCORRECTABLE, "Offline_Uncorrectable"
        ),
        crc_errors=_ata_attr(obj, ATA_ATTR_UDMA_CRC_ERRORS, "UDMA_CRC_Error_Count"),
        raw=obj,
    )


def detect_access_mode(runner: Runner, dev_path: str) -> list[str]:
    """Return the first ``-d`` arg set that yields a real report from ``smartctl``.

    Acceptance is by report content (model/serial present), not exit code:
    smartctl sets diagnostic bits in its status while still printing a full
    report, so a failing/aging drive on the correct bridge mode would be skipped
    if we trusted the exit code. Falls back to bare (``[]``) if none work, so the
    caller can still try. Order matters: bare/auto is preferred over a bridge mode.
    """
    for mode in ACCESS_MODES:
        result = runner.run(["smartctl", "--json", "-i", *mode, dev_path])
        try:
            info = parse_smart_json(result.json())
        except ValueError:
            continue
        if info.has_report:
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
    plausibility window (MIN_PLAUSIBLE_TEMP_C..MAX_PLAUSIBLE_TEMP_C) rejects
    garbage from a flaky bridge.
    """
    temp: int | None = None
    # Resolve symlinks and match the real node name (like required_tools), so we
    # only reach for ``nvme`` on an actual NVMe device - a substring check would
    # try it on a non-NVMe path that merely contains "nvme", where the binary may
    # not even be installed.
    if is_nvme_target(dev_path):
        try:
            result = runner.run(["nvme", "smart-log", dev_path, "-o", "json"])
        except ToolNotFound:
            result = None
        if result is not None and result.ok:
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
