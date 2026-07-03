"""A thin, mockable seam around subprocess.

Every external command in the package goes through a :class:`Runner`. Production
code uses :class:`SubprocessRunner`; tests inject a fake runner that maps an
argv to a canned :class:`Result`, so no real command ever runs under test.

Keeping this the *single* place that touches ``subprocess`` is what makes the
rest of the package unit-testable.
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
        proc = subprocess.run(
            list(argv),
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
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
    still printing a complete JSON report.
    """
    result = runner.run(argv, timeout=timeout)
    return result.json()
