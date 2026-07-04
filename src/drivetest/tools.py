"""Check that the external CLIs we depend on are installed.

Reporting *all* missing tools at once (rather than failing on the first) saves
the user a round-trip when setting up a fresh machine or live USB.
"""

from __future__ import annotations

import os
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
    if is_nvme_target(dev_path):
        tools.append("nvme")
    return tools


def is_nvme_target(dev_path: str) -> bool:
    """True for an NVMe device node.

    Resolves a ``by-id``/``by-path`` symlink to the real node first, then matches
    the kernel ``/dev/nvmeXnY`` name. A plain ``"nvme" in dev_path`` would miss a
    ``by-path`` link (whose name starts with ``pci-...``) and could false-match an
    unrelated path whose parent directory merely contains ``nvme``.
    """
    return os.path.basename(os.path.realpath(dev_path)).startswith("nvme")


def missing_tools(required: Iterable[str], which: Which = shutil.which) -> list[str]:
    """Return the subset of ``required`` tools not found on ``PATH``.

    ``which`` is injectable so tests can simulate a machine with any subset of
    tools installed.
    """
    return [tool for tool in required if which(tool) is None]
