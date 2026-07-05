"""Build and run fio: write+verify regions and read benchmarks.

Two deliberately different output strategies (see the module design notes):

- **write+verify** streams fio's normal output with a live ETA line, because a
  full-drive pass is long and the user wants progress. Pass/fail comes from
  fio's exit code (``--verify_fatal=1`` -> non-zero on a verify mismatch), which
  is unambiguous. A monitor samples temperature alongside and aborts the run at
  the thermal ceiling before the enclosure can hard-disconnect.
- **read benchmarks** use ``--output-format=json`` for exact bandwidth/IOPS,
  parsed by field - robust against the ambiguities that trip up text scraping
  (the aggregate ``READ:`` line vs. the ``seqread`` job header, locale, etc.).

The pure pieces - argv construction, JSON parsing, region classification and
the monitor decision loop - are unit-tested; :class:`FioRunner` wires them to a
real subprocess and gets a light integration test against a scratch file.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, TextIO, cast

from .planning import Region
from .thermal import Temp, ThermalPolicy, exceeds_ceiling
from .units import KIB

# fio ETA options: force an ETA line every 30s even though stdout is captured
# (fio would otherwise stay silent until done), on its own line so it streams.
ETA_OPTS = ["--eta=always", "--eta-newline=30s"]

# Seconds to wait after SIGTERM before escalating to SIGKILL when stopping fio.
TERMINATE_GRACE_S = 10

# Seconds to wait for the output-drain thread to finish after fio has stopped.
# fio's stdout closes once it exits, so the drain normally ends promptly; this is
# a backstop against an unbounded hang if the pipe somehow stays open.
DRAIN_JOIN_TIMEOUT_S = 30

# Throughput is conventionally reported in decimal MB/s (10^6 B/s), distinct from
# the binary MiB (2^20) used for sizes elsewhere.
MB = 1_000_000


class RegionResult(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    OVERHEAT = "OVERHEAT"


def _ignore_sample(_temp: Temp) -> None:
    pass


class ReadKind(Enum):
    """A read benchmark variant; its value is the fio job name / log-file id."""

    SEQ = "seqread"
    RAND = "randread"

    @property
    def label(self) -> str:
        return _READ_SHAPES[self].label


@dataclass(frozen=True)
class _ReadShape:
    """The fio parameters and display label for one read benchmark variant."""

    bs: str
    iodepth: int
    rw: str
    runtime_s: int
    label: str


_READ_SHAPES: dict[ReadKind, _ReadShape] = {
    ReadKind.SEQ: _ReadShape(
        bs="1M", iodepth=32, rw="read", runtime_s=60, label="sequential read (1M, qd32, 60s)"
    ),
    ReadKind.RAND: _ReadShape(
        bs="4k", iodepth=64, rw="randread", runtime_s=30, label="random read (4k, qd64, 30s)"
    ),
}


@dataclass(frozen=True)
class ReadStats:
    """Parsed result of a read benchmark."""

    kind: ReadKind
    bw_bytes: int  # bandwidth, bytes/sec
    iops: float

    @property
    def bw_mb(self) -> float:
        return self.bw_bytes / MB


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


def build_read_argv(dev_path: str, kind: ReadKind) -> list[str]:
    """fio argv for a read benchmark (JSON output).

    The per-variant block size, queue depth, mode and runtime come from
    ``_READ_SHAPES``.
    """
    shape = _READ_SHAPES[kind]
    return [
        "fio",
        f"--name={kind.value}",
        f"--filename={dev_path}",
        "--ioengine=libaio",
        "--direct=1",
        f"--bs={shape.bs}",
        f"--iodepth={shape.iodepth}",
        f"--rw={shape.rw}",
        f"--runtime={shape.runtime_s}",
        "--time_based",
        "--size=100%",
        "--group_reporting",
        "--output-format=json",
    ]


class FioReadError(ValueError):
    """A read benchmark's fio job reported a non-zero ``error``.

    A ``ValueError`` subclass so existing ``except ValueError`` callers still
    treat it as an unusable result, but a distinct type so the caller can tell a
    real read IO error (e.g. unreadable sectors) apart from unparseable output
    and surface it instead of silently logging "could not parse".
    """


def parse_read_json(obj: Any, kind: ReadKind) -> ReadStats:
    """Extract bandwidth and IOPS from fio's JSON for a read job.

    Raises :class:`FioReadError` if the job failed (non-zero ``error``), or a
    plain ``ValueError`` if the output is malformed (not an object, no jobs, a
    non-object job) or the numbers are absent: a missing figure means the
    benchmark did not produce a result, which must not be reported as a genuine
    0 B/s. Failing closed as ``ValueError`` (not a bare ``AttributeError``) keeps
    it inside the caller's ``except ValueError`` net.
    """
    if not isinstance(obj, dict):
        raise ValueError("fio JSON is not an object")
    top = cast("dict[str, Any]", obj)
    jobs: list[Any] = top.get("jobs") or []
    if not jobs:
        raise ValueError("fio JSON has no jobs")
    if not isinstance(jobs[0], dict):
        raise ValueError("fio JSON job is not an object")
    job = cast("dict[str, Any]", jobs[0])
    if job.get("error"):
        raise FioReadError(f"fio read job reported error {job['error']}")
    read: dict[str, Any] = job.get("read") or {}
    bw_bytes = read.get("bw_bytes")
    if bw_bytes is None:
        bw = read.get("bw")  # older fio reports bw in KiB/s only
        if bw is None:
            raise ValueError("fio JSON read section has no bandwidth")
        bw_bytes = bw * KIB
    iops = read.get("iops")
    if iops is None:
        raise ValueError("fio JSON read section has no iops")
    return ReadStats(kind=kind, bw_bytes=int(bw_bytes), iops=float(iops))


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
    on_sample: Callable[[Temp], None],
) -> bool:
    """Poll temperature while a region runs; kill fio at the ceiling.

    Returns True if the run was killed for overheating. Pure w.r.t. effects:
    all of process liveness, temperature, sleeping and killing are injected, so
    the ceiling logic is tested without a real process.
    """
    observe = on_sample
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
    """Runs fio write+verify regions (the streamed, temperature-monitored pass).

    Read benchmarks don't need this machinery - they run via a plain
    :class:`~drivetest.proc.Runner` and :func:`parse_read_json` - so this class is
    only the write path. Effects are injected for testability: ``popen`` creates
    the process, ``read_temp``/``sleep`` drive the monitor, and ``echo`` receives
    streamed output lines.
    """

    def __init__(
        self,
        *,
        read_temp: Callable[[], Temp],
        policy: ThermalPolicy,
        sleep: Callable[[float], None],
        popen: PopenFactory,
        echo: Callable[[str], None],
        on_sample: Callable[[Temp], None] | None = None,
    ) -> None:
        self._read_temp = read_temp
        self._policy = policy
        self._sleep = sleep
        self._popen = popen
        self._echo = echo
        # on_sample is the one effect production leaves defaulted (the orchestrator
        # doesn't observe per-sample temps here), so it keeps a no-op default.
        self._on_sample = on_sample or _ignore_sample

    def run_region(self, dev_path: str, region: Region, log_path: Path) -> RegionResult:
        """Write+verify one region, streaming output to console and ``log_path``,
        while monitoring temperature and aborting at the ceiling.
        """
        argv = build_writeverify_argv(dev_path, region)

        # Drain fio's combined output in a thread so the main thread is free to
        # poll temperature and kill on a ceiling breach. Any error draining (e.g.
        # the log disk fills) is recorded, not swallowed: it means the evidence
        # log is incomplete, so run_region must not report a PASS on it.
        drain_error: list[Exception] = []

        def drain(stream: TextIO, sink: TextIO) -> None:
            try:
                for line in stream:
                    sink.write(line)
                    sink.flush()
                    self._echo(line)
            except Exception as exc:
                drain_error.append(exc)

        # Open the log *before* launching fio: Popen begins writing the raw device
        # immediately, so if the open failed after it, we would leave a destructive
        # write running with nothing draining or stopping it.
        with open(log_path, "w") as logf:
            proc = self._popen(argv)
            reader: threading.Thread | None = None
            # This finally runs before the log file closes, on every path (Ctrl-C,
            # an error in the monitor, a kill): it stops fio - so we never leave a
            # write running against the device - and only then joins the drain
            # thread, so the thread can't write to a closed log (which would drop
            # trailing output) and no line is lost.
            try:
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
                returncode = proc.wait()
            finally:
                if proc.poll() is None:
                    self._terminate(proc)
                if reader is not None:
                    reader.join(DRAIN_JOIN_TIMEOUT_S)
        # fio has stopped and its output is drained; if the drainer could not keep
        # up or died, the log is untrustworthy - fail closed rather than classify.
        if reader is not None and reader.is_alive():
            raise RuntimeError(f"fio output drain did not finish within {DRAIN_JOIN_TIMEOUT_S}s")
        if drain_error:
            raise drain_error[0]
        return classify_region(overheat, returncode)

    @staticmethod
    def _terminate(proc: subprocess.Popen[str]) -> None:
        """Stop fio, escalating SIGTERM -> SIGKILL if it lingers."""
        proc.terminate()
        try:
            proc.wait(timeout=TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            # Reap the killed process so it can't linger as a zombie on the paths
            # where run_region does not wait() again (e.g. termination from the
            # exception-cleanup finally rather than the normal overheat path).
            proc.wait()
