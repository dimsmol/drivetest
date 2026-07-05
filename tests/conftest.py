"""Shared test helpers: a fake command runner and fixture loading."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from drivetest.proc import ProcTimeout, Result, ToolNotFound

FIXTURES = Path(__file__).parent / "fixtures"


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def load_json(name: str) -> Any:
    return json.loads(load_text(name))


@dataclass(frozen=True)
class Call:
    """One recorded invocation - argv plus any stdin/timeout, so tests can assert
    what was actually sent to a command, not just its name.
    """

    argv: tuple[str, ...]
    input: str | None = None
    timeout: float | None = None


@dataclass
class _Rule:
    tool: str
    contains: tuple[str, ...]
    result: Result
    error: BaseException | None = None

    def matches(self, argv: list[str]) -> bool:
        if not argv or argv[0] != self.tool:
            return False
        return all(any(tok in a for a in argv) for tok in self.contains)


@dataclass
class FakeRunner:
    """A :class:`drivetest.proc.Runner` that replays canned results.

    Register responses with :meth:`add`; matching is by executable name plus a
    set of substrings that must each appear somewhere in the argv. Matching is
    loose on purpose: ``contains=["-i"]`` matches the token ``-i`` but also an
    ``-i`` embedded in a longer arg, so keep tokens specific. Rules are tried in
    registration order; an unmatched command raises, so tests never silently
    pass on an unexpected call.

    Pass ``error=`` to make a matching rule raise instead of returning. Like the
    real :class:`drivetest.proc.SubprocessRunner`, a raw ``FileNotFoundError`` (or
    any other ``OSError``, e.g. a non-executable tool) is translated to
    ``ToolNotFound`` and ``subprocess.TimeoutExpired`` to ``ProcTimeout`` before it
    reaches the caller, so tests see exactly the exception types production code
    sees (callers never handle the raw stdlib exceptions). Any other ``error=`` is
    raised as-is. Every attempted call is recorded in
    :attr:`calls` (argv, input, timeout).
    """

    rules: list[_Rule] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def add(
        self,
        tool: str,
        *,
        contains: Sequence[str] = (),
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        error: BaseException | None = None,
    ) -> FakeRunner:
        self.rules.append(
            _Rule(
                tool=tool,
                contains=tuple(contains),
                result=Result(argv=(), returncode=returncode, stdout=stdout, stderr=stderr),
                error=error,
            )
        )
        return self

    def record(
        self, argv: Sequence[str], *, input: str | None = None, timeout: float | None = None
    ) -> list[str]:
        """Record a call and return the argv as a list (for subclasses that
        intercept commands before delegating to :meth:`run`).
        """
        argv_list = list(argv)
        self.calls.append(Call(tuple(argv_list), input, timeout))
        return argv_list

    def run(
        self,
        argv: Sequence[str],
        *,
        input: str | None = None,
        timeout: float | None = None,
    ) -> Result:
        argv_list = self.record(argv, input=input, timeout=timeout)
        for rule in self.rules:
            if rule.matches(argv_list):
                if rule.error is not None:
                    raise self._translate(rule.error, argv_list, timeout)
                return replace(rule.result, argv=tuple(argv_list))
        raise AssertionError(f"FakeRunner: no rule for {argv_list}")

    @staticmethod
    def _translate(
        error: BaseException, argv: list[str], timeout: float | None
    ) -> BaseException:
        """Mirror SubprocessRunner's exception translation so the fake surfaces
        the same error types production callers see.
        """
        if isinstance(error, FileNotFoundError):
            return ToolNotFound(argv)
        if isinstance(error, subprocess.TimeoutExpired):
            return ProcTimeout(argv, timeout)
        # A tool present but not executable (EACCES) or a bad path component
        # (ENOTDIR) is another OSError subclass; SubprocessRunner maps it to
        # ToolNotFound too, so the fake must as well to stay faithful.
        if isinstance(error, OSError):
            return ToolNotFound(argv)
        return error


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()


def collect_sleep() -> tuple[Callable[[float], None], list[float]]:
    """A fake sleep that records durations instead of waiting."""
    slept: list[float] = []
    return (lambda seconds: slept.append(seconds)), slept
