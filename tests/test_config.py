"""Tests for RunConfig's boundary invariants."""

from __future__ import annotations

import pytest

from drivetest.config import DEFAULT_QUICK_BYTES, DEFAULT_THERMAL_POLICY, RunConfig


def _config(*, parts: int = 1, quick_bytes: int = DEFAULT_QUICK_BYTES) -> RunConfig:
    return RunConfig(
        device="/dev/sdb", write=False, quick=False, force=False, only=None,
        assume_yes=False, log_dir=None, parts=parts, quick_bytes=quick_bytes,
        policy=DEFAULT_THERMAL_POLICY,
    )


def test_valid_config_constructs():
    assert _config().parts == 1


def test_rejects_nonpositive_parts():
    # The orchestrator's region math assumes parts >= 1; enforce it at the boundary
    # so a config from any source (not just the vetted CLI) fails closed here.
    with pytest.raises(ValueError):
        _config(parts=0)
    with pytest.raises(ValueError):
        _config(parts=-1)


def test_rejects_nonpositive_quick_bytes():
    with pytest.raises(ValueError):
        _config(quick_bytes=0)
    with pytest.raises(ValueError):
        _config(quick_bytes=-1)
