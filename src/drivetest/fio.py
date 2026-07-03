"""Build and run fio: write+verify regions and read benchmarks.

Two deliberately different output strategies (see the module design notes):

- **write+verify** streams fio's normal output with a live ETA line, because a
  full-drive pass is long and the user wants progress. Pass/fail comes from
  fio's exit code (``--verify_fatal=1`` -> non-zero on a verify mismatch), which
  is unambiguous. A monitor samples temperature alongside and aborts the run at
  the thermal ceiling before the enclosure can hard-disconnect.
- **read benchmarks** use ``--output-format=json`` for exact bandwidth/IOPS,
  parsed by field. This kills the old text-grep bugs (the aggregate ``READ:``
  line vs. the ``seqread`` header, etc.).

The pure pieces - argv construction, JSON parsing, region classification and
the monitor decision loop - are unit-tested; :class:`FioRunner` wires them to a
real subprocess and gets a light integration test against a scratch file.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO

from .planning import Region
from .thermal import Temp, ThermalPolicy, exceeds_ceiling

# fio ETA options: force an ETA line every 30s even though stdout is captured
# (fio would otherwise stay silent until done), on its own line so it streams.
ETA_OPTS = ["--eta=always", "--eta-newline=30s"]


class RegionResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    OVERHEAT = "OVERHEAT"


def _ignore_sample(_temp: Temp) -> None:
    pass


def _print_line(line: str) -> None:
    print(line, end="")


@dataclass(frozen=True)
class ReadStats:
    """Parsed result of a read benchmark."""

    kind: str            # "seqread" | "randread"
    bw_bytes: int        # bandwidth, bytes/sec
    iops: float

    @property
    def bw_mb(self) -> float:
        return self.bw_bytes / 1_000_000


def build_writeverify_argv(dev_path: str, region: Region) -> list[str]:
    """fio argv for a crc32c write+verify over one region (normal output)."""
    return [
        "fio",
        "--name=writeverify",
        f"--filename={dev_path}",
        "--ioengine=libaio",
        "--direct=1",
        "--bs=1M",
        "--iodepth=16",
        "--rw=write",
        "--verify=crc32c",
        "--do_verify=1",
        "--verify_fatal=1",
        "--verify_state_save=0",
        f"--offset={region.offset}",
        f"--size={region.size}",
        "--group_reporting",
        *ETA_OPTS,
    ]


def build_read_argv(dev_path: str, kind: str) -> list[str]:
    """fio argv for a read benchmark (JSON output).

    ``kind`` is "seqread" (1M, qd32, 60s) or "randread" (4k, qd64, 30s).
    """
    if kind == "seqread":
        shape = ["--bs=1M", "--iodepth=32", "--rw=read", "--runtime=60"]
    elif kind == "randread":
        shape = ["--bs=4k", "--iodepth=64", "--rw=randread", "--runtime=30"]
    else:
        raise ValueError(f"unknown read kind: {kind}")
    return [
        "fio",
        f"--name={kind}",
        f"--filename={dev_path}",
        "--ioengine=libaio",
        "--direct=1",
        *shape,
        "--time_based",
        "--size=100%",
        "--group_reporting",
        "--output-format=json",
    ]


def parse_read_json(obj: dict[str, Any], kind: str) -> ReadStats:
    """Extract bandwidth and IOPS from fio's JSON for a read job."""
    jobs: list[Any] = obj.get("jobs") or []
    if not jobs:
        raise ValueError("fio JSON has no jobs")
    job: dict[str, Any] = jobs[0] or {}
    read: dict[str, Any] = job.get("read") or {}
    bw_bytes = read.get("bw_bytes")
    if bw_bytes is None:
        # Older fio reports bw in KiB/s only.
        bw_bytes = (read.get("bw") or 0) * 1024
    return ReadStats(kind=kind, bw_bytes=int(bw_bytes), iops=float(read.get("iops") or 0.0))


def classify_region(overheat: bool, returncode: int) -> RegionResult:
    """Map (overheated?, fio exit code) to a region result."""
    if overheat:
        return RegionResult.OVERHEAT
    return RegionResult.PASS if returncode == 0 else RegionResult.FAIL


def monitor_region(
    *,
    is_alive: Callable[[], bool],
    read_temp: Callable[[], Temp],
    policy: ThermalPolicy,
    sleep: Callable[[float], None],
    kill: Callable[[], None],
    on_sample: Callable[[Temp], None] | None = None,
) -> bool:
    """Poll temperature while a region runs; kill fio at the ceiling.

    Returns True if the run was killed for overheating. Pure w.r.t. effects:
    all of process liveness, temperature, sleeping and killing are injected, so
    the ceiling logic is tested without a real process.
    """
    observe = on_sample or _ignore_sample
    while is_alive():
        temp = read_temp()
        observe(temp)
        if exceeds_ceiling(temp, policy):
            kill()
            return True
        sleep(policy.poll_interval_s)
    return False


PopenFactory = Callable[[list[str]], "subprocess.Popen[str]"]


def default_popen(argv: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


class FioRunner:
    """Runs fio for write+verify regions and read benchmarks.

    Effects are injected for testability: ``popen`` creates the process,
    ``read_temp``/``sleep`` drive the monitor, and ``echo`` receives streamed
    output lines (default: print to stdout).
    """

    def __init__(
        self,
        *,
        read_temp: Callable[[], Temp],
        policy: ThermalPolicy,
        sleep: Callable[[float], None] = time.sleep,
        popen: PopenFactory = default_popen,
        echo: Callable[[str], None] | None = None,
        run_json: Callable[[list[str]], dict[str, Any]] | None = None,
        on_sample: Callable[[Temp], None] | None = None,
    ) -> None:
        self._read_temp = read_temp
        self._policy = policy
        self._sleep = sleep
        self._popen = popen
        self._echo = echo or _print_line
        self._run_json = run_json
        self._on_sample = on_sample

    def run_region(self, dev_path: str, region: Region, log_path: Path) -> RegionResult:
        """Write+verify one region, streaming output to console and ``log_path``,
        while monitoring temperature and aborting at the ceiling.
        """
        argv = build_writeverify_argv(dev_path, region)
        proc = self._popen(argv)

        # Drain fio's combined output in a thread so the main thread is free to
        # poll temperature and kill on a ceiling breach.
        def drain(stream: TextIO, sink: TextIO) -> None:
            for line in stream:
                sink.write(line)
                sink.flush()
                self._echo(line)

        with open(log_path, "w") as logf:
            assert proc.stdout is not None
            reader = threading.Thread(target=drain, args=(proc.stdout, logf), daemon=True)
            reader.start()
            overheat = monitor_region(
                is_alive=lambda: proc.poll() is None,
                read_temp=self._read_temp,
                policy=self._policy,
                sleep=self._sleep,
                kill=lambda: self._terminate(proc),
                on_sample=self._on_sample,
            )
            reader.join()
            returncode = proc.wait()
        return classify_region(overheat, returncode)

    @staticmethod
    def _terminate(proc: subprocess.Popen[str]) -> None:
        """Stop fio, escalating SIGTERM -> SIGKILL if it lingers."""
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    def run_read(self, dev_path: str, kind: str) -> ReadStats:
        """Run a read benchmark and return parsed bandwidth/IOPS."""
        if self._run_json is None:
            raise RuntimeError("FioRunner needs run_json to run read benchmarks")
        obj = self._run_json(build_read_argv(dev_path, kind))
        return parse_read_json(obj, kind)
