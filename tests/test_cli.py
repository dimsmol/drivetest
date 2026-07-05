"""Tests for argument parsing and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from drivetest.cli import main, parse_args
from drivetest.config import DEFAULT_QUICK_BYTES, DEFAULT_THERMAL_POLICY
from drivetest.orchestrator import EXIT_REFUSED


def test_minimal_readonly():
    opts = parse_args(["/dev/sdb"])
    assert opts.device == "/dev/sdb"
    assert not opts.write
    assert opts.parts == 1


def test_write_parts_only():
    opts = parse_args(["--write", "--parts", "8", "--only", "1-4", "/dev/sdb"])
    assert opts.write
    assert opts.parts == 8
    assert opts.only == "1-4"


def _assert_refused(argv: list[str]) -> None:
    """A usage error must exit EXIT_REFUSED (1), not argparse's native 2 (which
    would collide with EXIT_ATTENTION). Asserting the code - not just that some
    SystemExit fired - keeps these tests honest about *which* contract held.
    """
    with pytest.raises(SystemExit) as excinfo:
        parse_args(argv)
    assert excinfo.value.code == EXIT_REFUSED


def test_quick_requires_write():
    _assert_refused(["--quick", "/dev/sdb"])


def test_usage_error_exits_refused_not_two():
    # A bad flag combination exits EXIT_REFUSED (1), not argparse's default 2,
    # so it stays distinguishable from EXIT_ATTENTION (a run that needs attention).
    _assert_refused(["--quick", "/dev/sdb"])


def test_argparse_native_error_also_exits_refused():
    # Even an argparse-native error (missing positional) maps to EXIT_REFUSED.
    _assert_refused([])


def test_only_requires_write():
    _assert_refused(["--only", "1-4", "/dev/sdb"])


def test_only_rejects_quick_combo():
    _assert_refused(["--write", "--quick", "--only", "1", "/dev/sdb"])


def test_only_requires_explicit_parts():
    # --only without --parts would default parts to 1 and silently accept "--only 1"
    # as a single continuous full-drive write - defeating the paced resume the flag
    # exists for. It must be rejected, not quietly run unpaced.
    _assert_refused(["--write", "--only", "1", "/dev/sdb"])


def test_only_with_explicit_parts_is_accepted():
    opts = parse_args(["--write", "--parts", "4", "--only", "1-2", "/dev/sdb"])
    assert opts.parts == 4 and opts.only == "1-2"


def test_malformed_only_spec_exits_refused():
    # A malformed --only token reaches parse_only_spec, whose ValueError is remapped
    # to a usage error (EXIT_REFUSED), not a bare traceback.
    _assert_refused(["--write", "--parts", "4", "--only", "abc", "/dev/sdb"])


def test_only_spec_validated_against_parts():
    _assert_refused(["--write", "--parts", "4", "--only", "5", "/dev/sdb"])


def test_parts_must_be_positive():
    _assert_refused(["--write", "--parts", "0", "/dev/sdb"])


def test_parts_requires_write():
    # --parts only paces the write pass; it's meaningless (and silently ignored)
    # without --write.
    _assert_refused(["--parts", "8", "/dev/sdb"])


def test_explicit_default_parts_still_requires_write():
    # Even --parts 1 (equal to the default value) is rejected without --write, so
    # the "requires --write" rule applies to any *explicit* --parts and doesn't
    # silently depend on the default happening to be 1.
    _assert_refused(["--parts", "1", "/dev/sdb"])


def test_omitted_parts_defaults_to_one():
    # No --parts still resolves to the default of 1 (a single full-drive region).
    assert parse_args(["/dev/sdb"]).parts == 1


def test_parts_rejected_with_quick():
    # --quick writes a single region, so --parts would be silently discarded.
    _assert_refused(["--write", "--quick", "--parts", "8", "/dev/sdb"])


def test_write_force_quick_flags_map_into_config():
    opts = parse_args(["--write", "--force", "--quick", "/dev/sdb"])
    assert opts.write and opts.force and opts.quick
    assert opts.quick_bytes == DEFAULT_QUICK_BYTES
    assert opts.policy is DEFAULT_THERMAL_POLICY


def test_force_requires_write():
    _assert_refused(["--force", "/dev/sdb"])


def test_device_required():
    _assert_refused([])


def test_assume_yes_and_log_dir():
    opts = parse_args(["--write", "--assume-yes", "--log-dir", "/tmp/logs", "/dev/sdb"])
    assert opts.assume_yes
    assert opts.log_dir == Path("/tmp/logs")


def _run_capturing_workdir(monkeypatch) -> dict[str, Path]:
    """Patch out the root check and the real run; capture the run's workdir."""
    captured: dict[str, Path] = {}
    monkeypatch.setattr("drivetest.cli.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "drivetest.cli.run", lambda _opts, ctx: captured.setdefault("workdir", ctx.workdir) or 0
    )
    return captured


def test_main_wires_log_dir_into_workdir(monkeypatch):
    captured = _run_capturing_workdir(monkeypatch)
    main(["--write", "--assume-yes", "--log-dir", "/tmp/logs", "/dev/sdb"])
    assert captured["workdir"] == Path("/tmp/logs")


def test_main_defaults_workdir_to_cwd(monkeypatch):
    captured = _run_capturing_workdir(monkeypatch)
    main(["/dev/sdb"])
    assert captured["workdir"] == Path(".")


def test_main_refuses_when_not_root(monkeypatch):
    # A destructive tool must not proceed without root: main returns EXIT_REFUSED
    # and never reaches run(), regardless of the (valid) arguments.
    monkeypatch.setattr("drivetest.cli.os.geteuid", lambda: 1000)
    called: list[object] = []
    monkeypatch.setattr("drivetest.cli.run", lambda _opts, ctx: called.append(ctx) or 0)
    assert main(["/dev/sdb"]) == EXIT_REFUSED
    assert called == []  # never reached the run
