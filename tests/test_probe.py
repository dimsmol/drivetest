"""Tests for the IO probes that feed the safety guards."""

from __future__ import annotations

from drivetest.devices import Device
from drivetest.probe import gather_blank_probe, gather_root_info

from .conftest import FakeRunner, load_text


def _disk(name="sdx", children=()):
    return Device(path=f"/dev/{name}", name=name, type="disk", size=100, children=children)


def test_blank_probe_on_empty_disk(fake_runner: FakeRunner, tmp_path):
    fake_runner.add("wipefs", stdout='{"signatures": []}')
    probe = gather_blank_probe(fake_runner, _disk(), sys_block=str(tmp_path))
    assert probe.is_blank
    assert not probe.probe_error


def test_blank_probe_detects_signatures(fake_runner: FakeRunner, tmp_path):
    fake_runner.add("wipefs", stdout=load_text("wipefs_signatures.json"))
    probe = gather_blank_probe(fake_runner, _disk(), sys_block=str(tmp_path))
    assert not probe.is_blank
    assert "ntfs" in probe.signatures
    assert "dos" in probe.signatures


def test_blank_probe_fails_closed_on_wipefs_error(fake_runner: FakeRunner, tmp_path):
    fake_runner.add("wipefs", stdout="", returncode=1)
    probe = gather_blank_probe(fake_runner, _disk(), sys_block=str(tmp_path))
    assert probe.probe_error
    assert not probe.is_blank


def test_blank_probe_reports_holders(fake_runner: FakeRunner, tmp_path):
    holders = tmp_path / "sdx" / "holders"
    holders.mkdir(parents=True)
    (holders / "dm-0").mkdir()
    fake_runner.add("wipefs", stdout='{"signatures": []}')
    probe = gather_blank_probe(fake_runner, _disk(), sys_block=str(tmp_path))
    assert probe.holders == ("dm-0",)
    assert not probe.is_blank


def test_blank_probe_reports_children(fake_runner: FakeRunner, tmp_path):
    child = Device(path="/dev/sdx1", name="sdx1", type="part", size=50)
    fake_runner.add("wipefs", stdout='{"signatures": []}')
    probe = gather_blank_probe(fake_runner, _disk(children=(child,)), sys_block=str(tmp_path))
    assert probe.children == ("sdx1",)


def test_root_info_resolves_parent_disks(fake_runner: FakeRunner):
    fake_runner.add(
        "findmnt",
        stdout='{"filesystems": [{"source": "/dev/nvme0n1p4", "target": "/"}]}',
    )
    fake_runner.add("lsblk", stdout="nvme0n1p4\nnvme0n1\n")
    root = gather_root_info(fake_runner)
    assert root.resolved
    assert "nvme0n1" in root.parent_disks


def test_root_info_strips_btrfs_subvolume(fake_runner: FakeRunner):
    fake_runner.add(
        "findmnt",
        stdout='{"filesystems": [{"source": "/dev/sda2[/@root]", "target": "/"}]}',
    )
    fake_runner.add("lsblk", stdout="sda2\nsda\n")
    root = gather_root_info(fake_runner)
    assert root.source == "/dev/sda2"
    assert "sda" in root.parent_disks


def test_root_info_unresolved_for_non_block_source(fake_runner: FakeRunner):
    fake_runner.add(
        "findmnt",
        stdout='{"filesystems": [{"source": "rpool/root", "target": "/"}]}',
    )
    root = gather_root_info(fake_runner)
    assert not root.resolved
    assert root.source == "rpool/root"


def test_root_info_unresolved_when_findmnt_fails(fake_runner: FakeRunner):
    fake_runner.add("findmnt", stdout="", returncode=1)
    root = gather_root_info(fake_runner)
    assert not root.resolved
