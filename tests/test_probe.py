"""Tests for the IO probes that feed the safety guards."""

from __future__ import annotations

from drivetest.devices import Device
from drivetest.probe import gather_blank_probe, gather_root_info

from .conftest import FakeRunner, load_text


def _disk(name="sdx", children=()):
    return Device(path=f"/dev/{name}", name=name, type="disk", size=100, children=children)


def _sys_block(tmp_path, *, name="sdx", holders=()) -> str:
    """A realistic /sys/block root: the device dir with a (maybe populated)
    holders/ subdir, as a real whole disk always has. Returned as a path string.
    """
    hdir = tmp_path / name / "holders"
    hdir.mkdir(parents=True)
    for h in holders:
        (hdir / h).mkdir()
    return str(tmp_path)


def test_blank_probe_on_empty_disk(fake_runner: FakeRunner, tmp_path):
    fake_runner.add("wipefs", stdout='{"signatures": []}')
    probe = gather_blank_probe(fake_runner, _disk(), sys_block=_sys_block(tmp_path))
    assert probe.is_blank
    assert not probe.probe_error


def test_blank_probe_detects_signatures(fake_runner: FakeRunner, tmp_path):
    fake_runner.add("wipefs", stdout=load_text("wipefs_signatures.json"))
    probe = gather_blank_probe(fake_runner, _disk(), sys_block=_sys_block(tmp_path))
    assert not probe.is_blank
    assert "ntfs" in probe.signatures
    assert "dos" in probe.signatures


def test_blank_probe_fails_closed_on_wipefs_error(fake_runner: FakeRunner, tmp_path):
    fake_runner.add("wipefs", stdout="", returncode=1)
    probe = gather_blank_probe(fake_runner, _disk(), sys_block=_sys_block(tmp_path))
    assert probe.probe_error
    assert not probe.is_blank


def test_blank_probe_fails_closed_when_sys_dir_missing(fake_runner: FakeRunner, tmp_path):
    # The device's /sys entry is absent - an abnormal state we must not read as
    # "no holders, looks blank".
    fake_runner.add("wipefs", stdout='{"signatures": []}')
    probe = gather_blank_probe(fake_runner, _disk(), sys_block=str(tmp_path))
    assert probe.probe_error
    assert not probe.is_blank


def test_blank_probe_fails_closed_on_malformed_wipefs_json(fake_runner: FakeRunner, tmp_path):
    # wipefs exits 0 but prints non-JSON: we can't confirm the disk is blank, so
    # the guard must fail closed rather than assume "no signatures".
    fake_runner.add("wipefs", stdout="not json at all")
    probe = gather_blank_probe(fake_runner, _disk(), sys_block=_sys_block(tmp_path))
    assert probe.probe_error
    assert not probe.is_blank


def test_blank_probe_fails_closed_on_non_object_wipefs_json(fake_runner: FakeRunner, tmp_path):
    # Valid JSON that isn't an object (null/list/number): .get would raise, so the
    # guard must treat it as an errored probe, not "no signatures, looks blank".
    fake_runner.add("wipefs", stdout="null")
    probe = gather_blank_probe(fake_runner, _disk(), sys_block=_sys_block(tmp_path))
    assert probe.probe_error
    assert not probe.is_blank


def test_blank_probe_reports_holders(fake_runner: FakeRunner, tmp_path):
    fake_runner.add("wipefs", stdout='{"signatures": []}')
    probe = gather_blank_probe(
        fake_runner, _disk(), sys_block=_sys_block(tmp_path, holders=("dm-0",))
    )
    assert probe.holders == ("dm-0",)
    assert not probe.is_blank


def test_blank_probe_reports_children(fake_runner: FakeRunner, tmp_path):
    child = Device(path="/dev/sdx1", name="sdx1", type="part", size=50)
    fake_runner.add("wipefs", stdout='{"signatures": []}')
    probe = gather_blank_probe(
        fake_runner, _disk(children=(child,)), sys_block=_sys_block(tmp_path)
    )
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


def test_root_info_unresolved_on_non_object_findmnt_json(fake_runner: FakeRunner):
    # findmnt prints valid JSON that isn't an object: .get would raise, so treat
    # the root as unresolved rather than crash.
    fake_runner.add("findmnt", stdout="null")
    root = gather_root_info(fake_runner)
    assert not root.resolved


def test_root_info_unresolved_when_lsblk_walk_fails(fake_runner: FakeRunner):
    # findmnt names a /dev source but the lsblk parent walk fails: we know the
    # source but can't map it to a disk, so the root is not established.
    fake_runner.add(
        "findmnt",
        stdout='{"filesystems": [{"source": "/dev/sda2", "target": "/"}]}',
    )
    fake_runner.add("lsblk", stdout="", returncode=1)
    root = gather_root_info(fake_runner)
    assert not root.resolved
    assert root.source == "/dev/sda2"


def test_root_info_unresolved_when_walk_is_empty(fake_runner: FakeRunner):
    # lsblk succeeds but names no disk: nothing to compare against, so the root
    # is not established - must fail closed as unresolved, not clean.
    fake_runner.add(
        "findmnt",
        stdout='{"filesystems": [{"source": "/dev/sda2", "target": "/"}]}',
    )
    fake_runner.add("lsblk", stdout="\n  \n")
    root = gather_root_info(fake_runner)
    assert not root.resolved
    assert root.parent_disks == ()
