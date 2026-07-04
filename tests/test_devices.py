"""Tests for lsblk parsing and device identity."""

from __future__ import annotations

import json

import pytest

from drivetest.devices import (
    all_serials,
    canonical_path,
    find_device,
    list_devices,
    parse_lsblk,
)

from .conftest import FakeRunner, load_text


def test_parse_usb_disk():
    [dev] = parse_lsblk(load_text("lsblk_usb_sda.json"))
    assert dev.path == "/dev/sda"
    assert dev.is_disk
    assert dev.size == 2000398934016
    assert dev.serial == "TAD0NT005915"
    assert dev.model == "ESD-S1C"
    assert dev.tran == "usb"
    assert dev.wwn is None
    assert dev.mountpoints == ()  # the single null mountpoint is dropped


def test_parse_nested_children_and_mountpoints():
    [dev] = parse_lsblk(load_text("lsblk_nvme_system.json"))
    assert len(dev.children) == 2
    # mountpoints are collected across the whole tree
    assert set(dev.all_mountpoints) == {"/boot", "/"}
    # walk yields the disk plus both partitions
    assert [d.name for d in dev.walk()] == ["nvme0n1", "nvme0n1p1", "nvme0n1p4"]


def test_identity_fingerprint_is_stable_and_distinct():
    # Parsed twice into separate objects: identity must be equal across parses
    # (that stability is the whole point of the fingerprint).
    [usb_a] = parse_lsblk(load_text("lsblk_usb_sda.json"))
    [usb_b] = parse_lsblk(load_text("lsblk_usb_sda.json"))
    [nvme] = parse_lsblk(load_text("lsblk_nvme_system.json"))
    assert usb_a.identity == usb_b.identity
    assert usb_a.identity != nvme.identity
    assert usb_a.identity == (usb_a.serial, usb_a.wwn, usb_a.size, usb_a.model)


def test_blank_fields_normalized_to_none():
    data = {
        "blockdevices": [
            {"name": "sdz", "path": "/dev/sdz", "type": "disk", "size": 100,
             "model": "   ", "serial": "", "wwn": None, "tran": "usb",
             "mountpoints": [None]}
        ]
    }
    [dev] = parse_lsblk(data)
    assert dev.model is None
    assert dev.serial is None


def test_list_and_find_device(fake_runner: FakeRunner):
    fake_runner.add("lsblk", stdout=load_text("lsblk_usb_sda.json"))
    devices = list_devices(fake_runner)
    assert devices[0].path == "/dev/sda"

    found = find_device(fake_runner, "/dev/sda")
    assert found.serial == "TAD0NT005915"


def test_all_serials_skips_empty():
    devices = parse_lsblk(load_text("lsblk_all.json"))
    serials = all_serials(devices)
    assert serials.count("DUP-SERIAL") == 2
    assert "S3ZHNF0KC28756" in serials


def test_partition_node_is_not_a_disk():
    # A partition must be distinguishable from a whole disk: the write guard keys
    # off is_disk, and a partition silently treated as a disk would be dangerous.
    [part] = parse_lsblk(
        {"blockdevices": [{"name": "sda1", "path": "/dev/sda1", "type": "part", "size": 100}]}
    )
    assert not part.is_disk
    assert part.type == "part"


def test_find_device_resolves_symlink_before_querying_lsblk(fake_runner: FakeRunner, tmp_path):
    # find_device must canonicalize a by-id/by-path symlink to the real node
    # before asking lsblk, so a symlinked target can't dodge later path checks.
    real = tmp_path / "nvme0n1"
    real.write_text("")
    link = tmp_path / "by-id-link"
    link.symlink_to(real)
    fake_runner.add("lsblk", stdout=load_text("lsblk_usb_sda.json"))
    find_device(fake_runner, str(link))
    queried = fake_runner.calls[-1].argv
    assert str(real) in queried  # lsblk saw the resolved node...
    assert str(link) not in queried  # ...not the symlink


# --- malformed / edge lsblk output ----------------------------------------

def test_parse_lsblk_rejects_non_object():
    with pytest.raises(ValueError):
        parse_lsblk("[]")
    with pytest.raises(ValueError):
        parse_lsblk("5")


def test_parse_lsblk_rejects_non_json():
    with pytest.raises(json.JSONDecodeError):
        parse_lsblk("not json at all")


def test_parse_lsblk_empty_when_no_blockdevices():
    assert parse_lsblk({}) == []
    assert parse_lsblk({"blockdevices": []}) == []


def test_find_device_raises_when_absent(fake_runner: FakeRunner):
    fake_runner.add("lsblk", stdout='{"blockdevices": []}')
    with pytest.raises(LookupError):
        find_device(fake_runner, "/dev/nope")


def test_size_parsed_from_string_and_missing():
    # lsblk versions vary: SIZE may arrive as a JSON string; a missing size is
    # kept as None (not faked to 0) so callers can fail loudly where it matters.
    data = {
        "blockdevices": [
            {"name": "sda", "path": "/dev/sda", "type": "disk", "size": "12345"},
            {"name": "sdb", "path": "/dev/sdb", "type": "disk", "size": None},
        ]
    }
    sda, sdb = parse_lsblk(data)
    assert sda.size == 12345
    assert sda.size_bytes == 12345
    assert sdb.size is None


def test_size_bytes_raises_when_absent():
    [dev] = parse_lsblk({"blockdevices": [{"name": "sdz", "path": "/dev/sdz", "type": "disk"}]})
    with pytest.raises(ValueError):
        _ = dev.size_bytes


def test_find_device_rejects_target_without_size(fake_runner: FakeRunner):
    # The target we're about to write to/benchmark must have a real size.
    fake_runner.add(
        "lsblk",
        stdout='{"blockdevices": [{"name": "sda", "path": "/dev/sda", "type": "disk"}]}',
    )
    with pytest.raises(LookupError):
        find_device(fake_runner, "/dev/sda")


def test_canonical_path_resolves_symlink(tmp_path):
    real = tmp_path / "sda"
    real.write_text("")
    link = tmp_path / "by-id-link"
    link.symlink_to(real)
    assert canonical_path(str(link)) == str(real)
