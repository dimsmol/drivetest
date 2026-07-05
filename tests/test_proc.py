"""Tests for the subprocess seam: Result, error translation, and run_json.

The error-translation cases run real (hermetic) subprocesses via the current
Python interpreter - the one place the suite must actually spawn a process, to
prove SubprocessRunner turns raw subprocess failures into this module's types.
"""

from __future__ import annotations

import json
import sys

import pytest

from drivetest.proc import (
    ProcError,
    ProcTimeout,
    Result,
    SubprocessRunner,
    ToolNotFound,
    run_json,
)

from .conftest import FakeRunner

# --- Result ---------------------------------------------------------------

def test_result_check_raises_on_failure_returns_self_on_success():
    ok = Result(argv=("x",), returncode=0, stdout="", stderr="")
    assert ok.check() is ok
    bad = Result(argv=("x",), returncode=1, stdout="", stderr="boom")
    with pytest.raises(ProcError):
        bad.check()


# --- SubprocessRunner error translation (real processes) ------------------

def test_missing_tool_raises_tool_not_found():
    with pytest.raises(ToolNotFound):
        SubprocessRunner().run(["drivetest-nonexistent-command-xyz"])


def test_timeout_raises_proc_timeout():
    with pytest.raises(ProcTimeout):
        SubprocessRunner().run(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5
        )


def test_nonzero_exit_is_captured_not_raised():
    # A non-zero exit is a normal Result (many tools set diagnostic bits), not an
    # exception - only genuine exec failures raise.
    result = SubprocessRunner().run(
        [sys.executable, "-c", "import sys; sys.stdout.write('hi'); sys.exit(3)"]
    )
    assert not result.ok
    assert result.returncode == 3
    assert result.stdout == "hi"


def test_empty_argv_raises_tool_not_found():
    # An empty argv would otherwise surface a bare IndexError from Popen; keep the
    # contract that callers only ever see this module's error types.
    with pytest.raises(ToolNotFound):
        SubprocessRunner().run([])


def test_non_executable_tool_raises_tool_not_found(tmp_path):
    # A file that exists but isn't executable raises PermissionError (EACCES) from
    # exec, not FileNotFoundError. It must still translate to ToolNotFound so an
    # unusable tool fails closed exactly like a missing one.
    tool = tmp_path / "not_exec"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o644)  # readable but not executable
    with pytest.raises(ToolNotFound):
        SubprocessRunner().run([str(tool)])


def test_bad_path_component_raises_tool_not_found():
    # A path whose parent is a file, not a directory, raises NotADirectoryError
    # (ENOTDIR) - another OSError subclass that must map to ToolNotFound.
    with pytest.raises(ToolNotFound):
        SubprocessRunner().run(["/etc/hostname/nope"])


def test_run_json_forwards_timeout_to_runner():
    # run_json must pass its timeout through to the runner (not silently drop it).
    runner = FakeRunner()
    runner.add("smartctl", stdout='{"a": 1}')
    run_json(runner, ["smartctl", "--json"], timeout=5)
    assert runner.calls[-1].timeout == 5


def test_stdin_is_forwarded_to_the_process():
    # SubprocessRunner forwards input= to the process's stdin.
    result = SubprocessRunner().run(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        input="ping",
    )
    assert result.stdout == "ping"


def test_stderr_is_captured():
    result = SubprocessRunner().run(
        [sys.executable, "-c", "import sys; sys.stderr.write('warn'); sys.exit(0)"]
    )
    assert result.ok
    assert result.stderr == "warn"


def test_undecodable_output_is_replaced_not_raised():
    # Non-UTF-8 bytes (a tool's stray error text) must decode with replacement
    # rather than crash the run - the deliberate errors="replace" choice.
    result = SubprocessRunner().run(
        [sys.executable, "-c", r"import sys; sys.stdout.buffer.write(b'\xff\xfe')"]
    )
    assert result.ok
    assert "\ufffd" in result.stdout  # the Unicode replacement character


# --- run_json -------------------------------------------------------------

def test_run_json_tolerates_nonzero_exit_with_valid_json():
    runner = FakeRunner()
    runner.add("smartctl", stdout='{"a": 1}', returncode=2)
    assert run_json(runner, ["smartctl", "--json"]) == {"a": 1}


def test_run_json_raises_proc_error_when_failed_and_not_json():
    runner = FakeRunner()
    runner.add("lsblk", stdout="lsblk: no such device", stderr="nope", returncode=1)
    with pytest.raises(ProcError):
        run_json(runner, ["lsblk", "-Jb", "/dev/nope"])


def test_run_json_reraises_decode_error_when_exit_ok():
    # Success but garbage output is a real decode problem, surfaced as such.
    runner = FakeRunner()
    runner.add("lsblk", stdout="not json", returncode=0)
    with pytest.raises(json.JSONDecodeError):
        run_json(runner, ["lsblk", "-Jb"])
