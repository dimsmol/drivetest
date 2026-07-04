"""A thin, mockable seam around subprocess.

Every external *query* command in the package goes through a :class:`Runner`.
Production code uses :class:`SubprocessRunner`; tests inject a fake runner that
maps an argv to a canned :class:`Result`, so no real command ever runs under
test. (The long-lived write+verify ``fio`` process is the deliberate exception:
:mod:`drivetest.fio` drives it via ``Popen`` so it can stream output and be
killed at the thermal ceiling.)

Failures are translated to this module's own error types (:class:`ToolNotFound`,
:class:`ProcTimeout`, :class:`ProcError`), so callers never have to import
``subprocess`` to handle them.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class ProcError(RuntimeError):
    """A command failed (non-zero exit) when the caller required success."""

    def __init__(self, result: Result) -> None:
        self.result = result
        cmd = " ".join(result.argv)
        super().__init__(f"command failed ({result.returncode}): {cmd}\n{result.stderr.strip()}")


class ToolNotFound(RuntimeError):
    """The external command's executable was not found on PATH."""

    def __init__(self, argv: Sequence[str]) -> None:
        self.argv = tuple(argv)
        name = self.argv[0] if self.argv else "?"
        super().__init__(f"command not found: {name} (is the tool installed and on PATH?)")


class ProcTimeout(RuntimeError):
    """A command did not finish within its timeout."""

    def __init__(self, argv: Sequence[str], timeout: float | None) -> None:
        self.argv = tuple(argv)
        self.timeout = timeout
        super().__init__(f"command timed out after {timeout}s: {' '.join(self.argv)}")


@dataclass(frozen=True)
class Result:
    """The outcome of running a command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check(self) -> Result:
        """Return self if the command succeeded, else raise :class:`ProcError`."""
        if not self.ok:
            raise ProcError(self)
        return self

    def json(self) -> Any:
        """Parse stdout as JSON. Raises ``json.JSONDecodeError`` on bad output."""
        return json.loads(self.stdout)


class Runner(Protocol):
    """Anything that can run an argv and return a :class:`Result`."""

    def run(
        self,
        argv: Sequence[str],
        *,
        input: str | None = None,
        timeout: float | None = None,
    ) -> Result: ...


class SubprocessRunner:
    """The real runner, backed by :mod:`subprocess`."""

    def run(
        self,
        argv: Sequence[str],
        *,
        input: str | None = None,
        timeout: float | None = None,
    ) -> Result:
        # An empty argv would raise a bare IndexError from Popen, bypassing this
        # module's error types; keep the "callers never see raw subprocess" contract.
        if not argv:
            raise ToolNotFound(argv)
        try:
            proc = subprocess.run(
                list(argv),
                input=input,
                capture_output=True,
                text=True,
                # Pin UTF-8 (not the locale encoding, which under a C/POSIX root
                # shell could raise on non-ASCII output); replace undecodable
                # bytes rather than crash on a tool's stray error text.
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ToolNotFound(argv) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProcTimeout(argv, timeout) from exc
        except OSError as exc:
            # A tool present but not executable (EACCES) or a bad path component
            # (ENOTDIR) raises another OSError subclass rather than
            # FileNotFoundError; translate it too so callers never see a raw
            # subprocess/OS error, and an unusable tool fails closed like a
            # missing one.
            raise ToolNotFound(argv) from exc
        return Result(
            argv=tuple(argv),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


def run_json(runner: Runner, argv: Sequence[str], *, timeout: float | None = None) -> Any:
    """Run a command that emits JSON on stdout and return the parsed object.

    Tolerates a non-zero exit as long as valid JSON was produced: several of our
    tools (notably ``smartctl``) set diagnostic bits in their exit status while
    still printing a complete JSON report. If the command both failed *and*
    produced no valid JSON, raises :class:`ProcError` (argv + stderr) rather than
    an opaque ``JSONDecodeError``.
    """
    result = runner.run(argv, timeout=timeout)
    try:
        return result.json()
    except json.JSONDecodeError:
        if not result.ok:
            raise ProcError(result) from None
        raise
