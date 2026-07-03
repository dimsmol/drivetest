"""Check that the external CLIs we depend on are installed.

Reporting *all* missing tools at once (rather than failing on the first) saves
the user a round-trip when setting up a fresh machine or live USB.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable

# Tools every run needs. ``nvme`` is added only for an NVMe target (see
# :func:`required_tools`) because a USB/SATA drive never uses it.
BASE_TOOLS: tuple[str, ...] = (
    "smartctl",
    "lsblk",
    "findmnt",
    "fio",
    "wipefs",
)

Which = Callable[[str], str | None]


def required_tools(dev_path: str) -> list[str]:
    """The tools needed to test ``dev_path`` (adds ``nvme`` for NVMe targets)."""
    tools = list(BASE_TOOLS)
    if "nvme" in dev_path:
        tools.append("nvme")
    return tools


def missing_tools(required: Iterable[str], which: Which = shutil.which) -> list[str]:
    """Return the subset of ``required`` tools not found on ``PATH``.

    ``which`` is injectable so tests can simulate a machine with any subset of
    tools installed.
    """
    return [tool for tool in required if which(tool) is None]
