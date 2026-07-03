"""Tests for lsblk parsing and device identity."""

from __future__ import annotations

from drivetest.devices import (
    all_serials,
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
    [usb] = parse_lsblk(load_text("lsblk_usb_sda.json"))
    [nvme] = parse_lsblk(load_text("lsblk_nvme_system.json"))
    assert usb.identity == usb.identity
    assert usb.identity != nvme.identity
    assert usb.serial is not None and usb.serial in usb.identity


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
