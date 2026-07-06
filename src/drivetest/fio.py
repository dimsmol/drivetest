"""Build and run fio: write+verify regions and read benchmarks.

Two deliberately different output strategies (see the module design notes):

- **write+verify** streams fio's normal output with a live ETA line, because a
  full-drive pass is long and the user wants progress. Pass/fail comes from
  fio's exit code (``--verify_fatal=1`` -> non-zero on a verify mismatch), which
  is unambiguous. A monitor samples temperature alongside and aborts the run at
  the thermal ceiling before the enclosure can hard-disconnect.
- **read benchmarks** use ``--output-format=json`` for exact bandwidth/IOPS,
  parsed by field.
"""

from __future__ import annotations

import shlex
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, TextIO, cast

from .planning import Region
from .thermal import Temp, ThermalPolicy, exceeds_ceiling
from .units import KIB, MIB

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

_FIO_OUTPUT_START = "== [fio output start] " + "=" * 50
_FIO_OUTPUT_END = "== [fio output end] " + "=" * 52


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


def build_writeverify_argv(dev_path: str, offset: int, size: int, bs: str = "1M") -> list[str]:
    """fio argv for a crc32c write+verify over ``[offset, offset+size)`` (normal output).

    ``bs`` is the block size, defaulting to 1 MiB for the bulk of a region. The
    caller passes a smaller ``bs`` equal to the remainder for a region's sub-MiB
    tail, which a 1 MiB job would otherwise round away (see
    :meth:`FioRunner.run_region`).
    """
    return [
        "fio",
        "--name=writeverify",
        f"--filename={dev_path}",
        "--ioengine=libaio",
        "--direct=1",
        f"--bs={bs}",
        "--iodepth=16",
        "--rw=write",
        "--verify=crc32c",
        "--do_verify=1",
        "--verify_fatal=1",
        "--verify_state_save=0",
        f"--offset={offset}",
        f"--size={size}",
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
    real read IO error (e.g. unreadable sectors) apart from unparsable output
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

        fio's 1 MiB job rounds a region's ``size`` down to a whole block, so a
        sub-MiB tail (only the last region of a not-MiB-aligned device has one)
        would go unwritten. To keep full-drive coverage the region runs as a
        1 MiB-block *body* plus, when the size isn't a whole MiB, a final short
        *tail* block (``bs`` = the sector-aligned remainder). Both stream to the
        same log; the region PASSes only if every pass does, and an OVERHEAT/FAIL
        on the body short-circuits the tail.
        """
        # A zero/negative region would produce no passes and fall through as a
        # bogus PASS - report nothing rather than silently "verify" empty space.
        # (plan_regions and quick_region already guarantee a positive size; this
        # is a fail-closed guard against an upstream regression.)
        if region.size <= 0:
            raise ValueError(f"region {region.index} has non-positive size {region.size}")

        body_size = region.size // MIB * MIB
        tail_size = region.size - body_size
        passes: list[tuple[int, int, str]] = []
        if body_size:
            passes.append((region.offset, body_size, "1M"))
        if tail_size:
            # A sub-MiB (but sector-aligned) trailing block, written whole so its
            # bytes are verified too; ``bs`` = its exact size means one IO.
            passes.append((region.offset + body_size, tail_size, str(tail_size)))

        # Open the log *before* launching any fio: Popen begins writing the raw
        # device immediately, so if the open failed after it we would leave a
        # destructive write running with nothing draining or stopping it. The
        # body and tail passes share the one open log.
        with open(log_path, "w") as logf:
            result = RegionResult.PASS
            for offset, size, bs in passes:
                result = self._run_argv(build_writeverify_argv(dev_path, offset, size, bs), logf)
                if result is not RegionResult.PASS:
                    break
        return result

    def _run_argv(self, argv: list[str], logf: TextIO) -> RegionResult:
        """Run one fio invocation, draining its output to the already-open ``logf``
        while the temperature monitor can kill it at the ceiling.

        ``logf`` is opened by :meth:`run_region` (so it exists before fio starts
        writing the device) and shared across a region's body/tail passes.
        """
        # Drain fio's combined output in a thread so the main thread is free to
        # poll temperature and kill on a ceiling breach. Any error draining (e.g.
        # the log disk fills) is recorded, not swallowed: it means the evidence
        # log is incomplete, so the pass must not be reported as a PASS.
        drain_error: list[Exception] = []

        def drain(stream: TextIO, sink: TextIO) -> None:
            try:
                for line in stream:
                    sink.write(line)
                    sink.flush()
                    self._echo(line)
            except Exception as exc:
                drain_error.append(exc)

        # Record the exact argv, then open the fence that wraps fio's own output.
        # Safe to write here: no drain thread runs yet, so nothing else is touching
        # the log. shlex.join quotes the argv into a copy-pasteable command line.
        logf.write(f"drivetest> command: {shlex.join(argv)}\n")
        logf.write(f"{_FIO_OUTPUT_START}\n")
        logf.flush()

        proc = self._popen(argv)
        reader: threading.Thread | None = None
        # The finally runs before the caller closes the log, on every path (Ctrl-C,
        # an error in the monitor, a kill): it stops fio - so we never leave a
        # write running against the device - and only then joins the drain thread,
        # so the thread can't write to a closed log (dropping trailing output).
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
            # The drain thread is still writing the log; do not append the exit
            # line ourselves and race it - the timeout below is the real outcome.
            raise RuntimeError(f"fio output drain did not finish within {DRAIN_JOIN_TIMEOUT_S}s")
        # Drain has finished, so we own the log again: close fio's output fence and
        # record its exit code. Written whether the pass passed or failed, so every
        # block is self-describing. The trailing blank line separates this block
        # from the next pass's command line (body from tail, region from region).
        logf.write(f"{_FIO_OUTPUT_END}\n")
        logf.write(f"drivetest> fio exited with code {returncode}\n")
        logf.write("\n")
        logf.flush()
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
