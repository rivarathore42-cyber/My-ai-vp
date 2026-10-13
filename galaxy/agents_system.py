"""System agents: native engine bridges (self-healing NDJSON subprocesses), self-evolution (real metrics and
bounded parameter tuning), debugger sentinel (SQLite error tracking) and image metadata."""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import logging
import os
import re
import struct
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from .core import PACKAGE_DIR, PARAMS, ROOT, TUNE_BOUNDS, Agent, AgentResult, fail, ok, system_metrics
from .memory import MemoryStore

log = logging.getLogger("galaxy.v6.system")
MAX_PRIMES_N = 5_000_000


import resource  # noqa: E402  Phase 3: engine containment
import signal  # noqa: E402
import sqlite3  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from subprocess import PIPE  # noqa: E402
from .native_ref import OPS as REFERENCE_OPS, run as reference_run  # noqa: E402
from . import vision  # noqa: E402  Phase 4: structural image and PDF inspection
from .telemetry import BUDGETS, budget_for, current_rss_mb, propose_budget, request_fits  # noqa: E402
from .numparse import MAX_NUMBERS, extract_numbers  # noqa: E402  RC-01: one number reader
from .breaker import CircuitBreaker  # noqa: E402  RC-04
import traceback  # noqa: E402

RESPONSE_LIMIT_BYTES = 64 * 1024
STDERR_KEEP_BYTES = 2000
ENGINE_ADDRESS_SPACE = 1024 * 1024 * 1024
ENGINE_CPU_SECONDS = 15
NATIVE_CALL_SECONDS = 8.0
POOL_DEFAULT = 2


class BridgeError(RuntimeError):
    """A native engine could not be reached or returned an unusable response. `kind` says why: unavailable,
    circuit_open, timeout, crashed, invalid or error. It never escapes an agent's process() method."""

    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


def _sanitize(raw, limit: int = 300) -> str:
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]", "", text).replace("\r", "")
    return text.strip()[-limit:]


def apply_engine_limits() -> None:
    """Runs in the child process before exec: caps address space and CPU time for every native engine."""
    resource.setrlimit(resource.RLIMIT_AS, (ENGINE_ADDRESS_SPACE, ENGINE_ADDRESS_SPACE))
    resource.setrlimit(resource.RLIMIT_CPU, (ENGINE_CPU_SECONDS, ENGINE_CPU_SECONDS))


@dataclass
class _Worker:
    proc: asyncio.subprocess.Process
    stderr: bytearray
    drain: asyncio.Future
    path: str = ""


async def _reap(proc) -> None:
    with contextlib.suppress(Exception):
        await proc.wait()


def parse_command(text: str, aliases: dict[str, str], supported: tuple[str, ...]) -> Optional[tuple[str, list[float]]]:
    """Finds the operation word and reads every number in the text with numparse (RC-01). Raises ValueError for a
    number that is out of range; it never splits '1e5' or '1,000' and never drops a number silently."""
    lowered = text.lower()
    for word in sorted(set(aliases) | set(supported), key=len, reverse=True):
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return aliases.get(word, word), extract_numbers(text)
    return None


class NativeBridge(Agent):
    """NDJSON over stdin/stdout to a compiled engine, served by a small pool of persistent processes.

    Containment: each engine runs in its own session with an address-space cap and a CPU cap; a call that exceeds its
    budget kills the whole process group; stderr is captured in a bounded buffer and sanitised; a response line over
    64 KB is refused; a non-JSON reply is refused. Failures feed a CircuitBreaker (RC-04): three consecutive engine
    failures open it for 10 s; then one probe is let through. A cancelled call kills its worker and frees its probe.
    A missing binary is reported as 'unavailable' and is not an engine failure."""

    name = "Native Bridge"
    engine_label = "native engine"
    example = "'sum 1 2 3'"
    supported_ops: tuple[str, ...] = ("sum", "mean", "stddev", "sort", "primes_upto")
    op_aliases: dict[str, str] = {}
    FAIL_LIMIT = 3
    COOLDOWN = 10.0

    def __init__(self) -> None:
        try:
            self.pool_size = max(1, min(8, int(os.environ.get("GALAXY_NATIVE_POOL", POOL_DEFAULT))))
        except ValueError:
            self.pool_size = POOL_DEFAULT
        self.store = None
        self.breaker = CircuitBreaker(self.engine_label, failure_threshold=self.FAIL_LIMIT,
                                      cooldown_s=self.COOLDOWN, clock=time.time)
        self._sem: Optional[asyncio.Semaphore] = None
        self._idle: list[_Worker] = []
        self.starts = 0
        self.calls = 0
        self.timeouts = 0
        self.crashes = 0
        self.last_kind = ""
        self.last_error = ""
        self.last_stderr = ""

    # compatibility surface used by the orchestrator, the debugger and the tests
    @property
    def tripped_until(self) -> float:
        return self.breaker.open_until()

    @tripped_until.setter
    def tripped_until(self, value: float) -> None:
        if value <= 0:
            self.breaker.reset()
        else:
            self.breaker.force_open(value)

    @property
    def consecutive_failures(self) -> int:
        return self.breaker.failures

    def circuit_open(self) -> bool:
        return self.breaker.is_open_now()

    def candidates(self) -> list[str]:
        return []

    def candidates_checked(self) -> list[str]:
        return [c for c in self.candidates() if c] or ["(no path configured)"]

    def binary(self) -> Optional[Path]:
        for cand in self.candidates():
            path = Path(cand)
            if cand and path.is_file() and os.access(path, os.X_OK):
                return path
        return None

    def _breaker_error(self) -> BridgeError:
        return BridgeError(f"{self.engine_label} paused after repeated failures; retry in "
                           f"{max(1, int(self.tripped_until - time.time()) + 1)}s", "circuit_open")

    def _flush_breaker(self) -> None:
        if self.store is None:
            return
        for state in self.breaker.drain_transitions():
            try:
                self.store.log_breaker_event(self.engine_label, state, self.breaker.failures)
            except sqlite3.Error:
                log.exception("could not write the breaker event")

    async def _spawn(self) -> _Worker:
        binary = self.binary()
        if binary is None:
            raise BridgeError(f"{self.engine_label} binary not found (build it or set its env override)", "unavailable")
        try:
            proc = await asyncio.create_subprocess_exec(
                str(binary), stdin=PIPE, stdout=PIPE, stderr=PIPE, start_new_session=True,
                limit=RESPONSE_LIMIT_BYTES, preexec_fn=apply_engine_limits)
        except OSError as exc:
            raise BridgeError(f"{self.engine_label} could not start ({type(exc).__name__})", "unavailable") from None
        self.starts += 1
        buf = bytearray()

        async def drain() -> None:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    return
                buf.extend(chunk)
                del buf[:-STDERR_KEEP_BYTES]

        return _Worker(proc, buf, asyncio.ensure_future(drain()), str(binary))

    def _kill(self, worker: _Worker) -> None:
        if worker.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(worker.proc.pid, signal.SIGKILL)
        asyncio.ensure_future(_reap(worker.proc))

    def _take_idle(self, path: str) -> Optional[_Worker]:
        while self._idle:
            candidate = self._idle.pop()
            if candidate.path == path and candidate.proc.returncode is None:
                return candidate
            self._kill(candidate)
            candidate.drain.cancel()
        return None

    def _drop_idle(self) -> None:
        for worker in self._idle:
            self._kill(worker)
            worker.drain.cancel()
        self._idle = []

    async def _discard(self, worker: _Worker, kind: str, message: str) -> None:
        self._kill(worker)
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(worker.drain), 0.5)
        worker.drain.cancel()
        self.last_kind = kind
        self.last_error = f"{kind}: {message}"[:200]
        self.last_stderr = _sanitize(worker.stderr)
        if kind == "timeout":
            self.timeouts += 1
        if kind == "crashed":
            self.crashes += 1
        self.breaker.record_failure()
        self._flush_breaker()

    def _release(self, worker: _Worker) -> None:
        if worker.proc.returncode is None and len(self._idle) < self.pool_size:
            self._idle.append(worker)
        else:
            self._kill(worker)
            worker.drain.cancel()

    async def _exchange(self, worker: _Worker, op: str, payload: list[float], timeout: float) -> dict:
        request = json.dumps({"id": uuid.uuid4().hex, "op": op, "payload": payload}) + "\n"
        try:
            worker.proc.stdin.write(request.encode())
            await worker.proc.stdin.drain()
            raw = await asyncio.wait_for(worker.proc.stdout.readline(), timeout)
        except asyncio.CancelledError:
            self._kill(worker)
            worker.drain.cancel()
            raise
        except asyncio.TimeoutError:
            await self._discard(worker, "timeout", f"no answer within {timeout:.1f} s")
            raise BridgeError(f"{self.engine_label} timed out after {timeout:.1f} s", "timeout") from None
        except ValueError:
            await self._discard(worker, "invalid", "response line exceeded the 64 KB limit")
            raise BridgeError(f"{self.engine_label} sent a response over 64 KB", "invalid") from None
        except (BrokenPipeError, ConnectionResetError) as exc:
            await self._discard(worker, "crashed", type(exc).__name__)
            raise BridgeError(f"{self.engine_label} crashed: {self.last_stderr or type(exc).__name__}", "crashed") from None
        if not raw:
            await self._discard(worker, "crashed", "engine closed its output")
            raise BridgeError(f"{self.engine_label} crashed: {self.last_stderr or 'engine closed its output'}",
                              "crashed") from None
        try:
            response = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            response = None
        if not isinstance(response, dict):
            await self._discard(worker, "invalid", "response was not a JSON object")
            raise BridgeError(f"{self.engine_label} returned an invalid response", "invalid") from None
        self.calls += 1
        self._release(worker)
        self.breaker.record_success()
        self._flush_breaker()
        return response

    async def call(self, op: str, payload: list[float], timeout: float) -> dict:
        if not self.breaker.allow():
            self._flush_breaker()
            raise self._breaker_error()
        self._flush_breaker()
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.pool_size)
        try:
            async with self._sem:
                binary = self.binary()
                if binary is None:
                    self._drop_idle()
                    raise BridgeError(f"{self.engine_label} binary not found (build it or set its env override)",
                                      "unavailable")
                worker = self._take_idle(str(binary))
                if worker is None:
                    worker = await self._spawn()
                return await self._exchange(worker, op, payload, timeout)
        except BridgeError as exc:
            if exc.kind in ("unavailable", "circuit_open"):
                self.breaker.release()
            self._flush_breaker()
            raise
        except BaseException:
            self.breaker.release()
            self._flush_breaker()
            raise

    def reset_after_repair(self) -> bool:
        """Called by the debugger after an error. True means the binary exists and the breaker is closed."""
        return self.binary() is not None and not self.circuit_open()

    def status(self) -> dict:
        binary = self.binary()
        return {"engine": self.engine_label, "binary_found": binary is not None,
                "binary": str(binary) if binary else None, "candidates": self.candidates_checked(),
                "pool_size": self.pool_size, "idle_workers": len(self._idle), "starts": self.starts,
                "calls": self.calls, "timeouts": self.timeouts, "crashes": self.crashes,
                "consecutive_failures": self.consecutive_failures, "circuit_open": self.circuit_open(),
                "breaker": self.breaker.snapshot(), "last_kind": self.last_kind, "last_error": self.last_error,
                "last_stderr": self.last_stderr}

    async def stop(self) -> None:
        self._drop_idle()

    def _log(self, op: str, status: str, n_inputs: int, started: float, excerpt: str = "") -> None:
        if self.store is None:
            return
        try:
            self.store.log_native_run(self.name, op, status, n_inputs, (time.perf_counter() - started) * 1000, excerpt)
        except sqlite3.Error:
            log.exception("could not write the native run log")

    async def unavailable_result(self, op: str, nums: list[float], reason: str) -> AgentResult:
        """What to do when the engine binary is absent. The default is an honest failure."""
        return fail(self.name, reason)

    async def process(self, text: str, ctx: dict) -> AgentResult:
        try:
            parsed = parse_command(text, self.op_aliases, self.supported_ops)
        except ValueError as exc:
            return fail(self.name, str(exc)[:200])
        if parsed is None:
            return fail(self.name, f"numbers ke saath command likhein, e.g. {self.example}")
        op, nums = parsed
        if op not in self.supported_ops:
            return fail(self.name, f"'{op}' is not implemented by the {self.engine_label}")
        if not nums:
            return fail(self.name, "numbers do, e.g. " + self.example)
        if len(nums) > MAX_NUMBERS:
            return fail(self.name, f"at most {MAX_NUMBERS:,} numbers per request")
        if op == "primes_upto" and not 0 <= nums[0] <= MAX_PRIMES_N:
            return fail(self.name, f"primes limit is 0..{MAX_PRIMES_N:,}")
        remaining = (ctx["deadline"] - time.monotonic()) if ctx.get("deadline") else NATIVE_CALL_SECONDS
        timeout = max(1.0, min(NATIVE_CALL_SECONDS, remaining))
        started = time.perf_counter()
        try:
            response = await self.call(op, nums, timeout)
        except BridgeError as exc:
            status = exc.kind if exc.kind in NATIVE_STATUSES else "error"
            self._log(op, status, len(nums), started, self.last_stderr if status == "crashed" else "")
            if exc.kind == "unavailable":
                return await self.unavailable_result(op, nums, str(exc))
            return fail(self.name, str(exc)[:300])
        if response.get("status") != "ok":
            self._log(op, "error", len(nums), started)
            return fail(self.name, str(response.get("error", "engine error"))[:200])
        self._log(op, "ok", len(nums), started)
        return ok(self.name, f"`{op}` = `{response.get('result')}`", engine=self.engine_label)


NATIVE_STATUSES = ("ok", "error", "timeout", "crashed", "invalid", "unavailable", "circuit_open", "fallback")


class CppNativeBridge(NativeBridge):
    name = "C++ Native Bridge"
    engine_label = "C++ engine"
    example = "'trace 1 2 3 4'"
    supported_ops = ("sum", "mean", "sort", "primes_upto", "segmented_sieve", "matrix_trace")
    op_aliases = {"trace": "matrix_trace", "primes": "primes_upto", "prime": "primes_upto"}

    def candidates(self) -> list[str]:
        return [os.environ.get("GALAXY_CPP_ENGINE", ""), str(ROOT / "cpp_engine" / "cpp_engine")]

    @staticmethod
    def compiler_status() -> dict:
        """Presence of the toolchain, reported without raising. Missing g++ is a status, not an error."""
        import shutil as _shutil
        gpp = _shutil.which("g++")
        return {"g++": gpp, "available": gpp is not None}


class RustSafetyBridge(NativeBridge):
    name = "Rust Safety Bridge"
    engine_label = "Rust engine"
    example = "'sum 4 8 15'"
    supported_ops = ("sum", "mean", "stddev", "sort", "primes_upto")

    def candidates(self) -> list[str]:
        return [os.environ.get("GALAXY_RUST_ENGINE", ""), str(ROOT / "rust_engine" / "target" / "release" / "rust_engine")]


class ZigKernelBridge(NativeBridge):
    """Speaks the same NDJSON protocol as the other engines. If the Zig binary is absent, the answer comes from the
    deterministic Python reference, labelled as such, together with a structured diagnostic. If the binary is present but
    fails, the failure is reported and never silently replaced."""

    name = "Zig Kernel Bridge"
    engine_label = "Zig kernel"
    example = "'sum 1 2'"
    supported_ops = ("sum", "mean", "sort")

    def candidates(self) -> list[str]:
        return [os.environ.get("GALAXY_ZIG_KERNEL", ""), str(ROOT / "zig_kernel" / "zig-out" / "bin" / "zig_kernel")]

    async def unavailable_result(self, op: str, nums: list[float], reason: str) -> AgentResult:
        if op not in REFERENCE_OPS or op not in self.supported_ops:
            return fail(self.name, f"{reason}; the Python reference has no '{op}' either")
        try:
            value = reference_run(op, nums)
        except ValueError as exc:
            return fail(self.name, f"{reason}; reference: {exc}")
        self._log(op, "fallback", len(nums), time.perf_counter())
        diagnostic = {"status": "unavailable", "reason": reason, "candidates": self.candidates_checked(),
                      "fallback": "python-reference"}
        return ok(self.name, f"`{op}` = `{value}`\n\n**Zig kernel diagnostic:** {reason}. The value was computed by "
                             "the deterministic Python reference, not by a native kernel.",
                  engine="python-reference", diagnostic=diagnostic)


def _p95(values: list[float]) -> Optional[float]:
    """The same nearest-rank p95 as the telemetry summary, so budgets and reports agree (A18-01)."""
    from .memory import _percentile
    return _percentile(sorted(values), 95)


class SelfEvolutionAgent(Agent):
    """Real machine metrics, per-agent telemetry (latency, queue wait, memory) and bounded, logged tuning. It never edits
    or reloads code. tune() moves the default timeout. tune_budgets() moves each agent's own budget from its p95 trend,
    within 5-45 s, by at most 20% (and 5 s) per change, and only when the request invariant still holds."""

    name = "Self-Evolution"
    MIN_SAMPLES = 30
    MIN_GAP_SECONDS = 60.0
    MIN_CHANGE_SECONDS = 1.0

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.started = time.time()

    def metrics(self) -> dict:
        return {"system": system_metrics(), "agent_calls_last_200": self.store.call_stats(window=200),
                "per_agent": self.store.agent_summary(window=200), "uptime_s": round(time.time() - self.started, 1),
                "parameters": dict(PARAMS), "budgets": dict(BUDGETS),
                "tuning_bounds": {k: list(v) for k, v in TUNE_BOUNDS.items()}}

    def tune(self) -> dict:
        stats = self.store.call_stats(window=200)
        if stats["n"] < self.MIN_SAMPLES or stats["p95_ms"] is None:
            return {"applied": False, "reason": f"need {self.MIN_SAMPLES} calls with successes, have {stats['n']}"}
        last = self.store.last_tuning_time()
        if last is not None and time.time() - last < self.MIN_GAP_SECONDS:
            return {"applied": False, "reason": "tuned less than 60 s ago"}
        low, high = TUNE_BOUNDS["agent_timeout"]
        target = round(min(high, max(low, stats["p95_ms"] / 1000 * 3)), 1)
        old = PARAMS["agent_timeout"]
        if abs(target - old) < self.MIN_CHANGE_SECONDS:
            return {"applied": False, "reason": f"target {target}s is within 1 s of the current {old}s"}
        PARAMS["agent_timeout"] = target
        self.store.log_tuning("agent_timeout", old, target,
                              f"p95 {stats['p95_ms']} ms over {stats['n']} calls, x3 headroom, bounded {low}-{high}")
        return {"applied": True, "param": "agent_timeout", "old": old, "new": target}

    def tune_budgets(self) -> dict:
        names = sorted(self.store.agent_summary(window=200).keys())
        current = {name: budget_for(name, PARAMS["agent_timeout"]) for name in names}
        proposed = dict(current)
        skipped = []
        for name in names:
            recent = self.store.latency_window(name, 0, 100)
            if len(recent) < 20:
                skipped.append(name)
                continue
            older = self.store.latency_window(name, 100, 100)
            previous = _p95(older) if len(older) >= 20 else None
            proposed[name] = propose_budget(current[name], _p95(recent), previous)
        if not request_fits(proposed):
            return {"applied": False, "reason": "proposed budgets would break the 60 s request invariant", "changes": []}
        changes = []
        for name in names:
            if abs(proposed[name] - current[name]) > 1e-9:
                BUDGETS[name] = proposed[name]
                self.store.log_tuning(f"budget:{name}", current[name], proposed[name],
                                      "p95 trend over the last 100 successful calls, bounded 20% and 5 s per change")
                changes.append({"agent": name, "old": current[name], "new": proposed[name]})
        return {"applied": bool(changes), "changes": changes, "skipped_too_few_samples": skipped}

    async def process(self, text: str, ctx: dict) -> AgentResult:
        if re.search(r"\btune\b|\boptimi[sz]e\b", text, re.I):
            result = self.tune()
            if result["applied"]:
                return ok(self.name, f"Tuned **{result['param']}**: {result['old']}s -> {result['new']}s (logged).")
            return ok(self.name, f"No change: {result['reason']}.")
        m = self.metrics()
        system = m["system"]
        calls = m["agent_calls_last_200"]
        rows = [
            f"| uptime | {m['uptime_s']} s |",
            f"| CPUs | {system['cpu_count']} |",
            f"| load (1 min) | {system['load_1m'] if system['load_1m'] is not None else 'n/a on this platform'} |",
            f"| memory available | {system['mem_available_mb'] if system['mem_available_mb'] is not None else 'n/a'} MB |",
            f"| peak RSS | {system['peak_rss_mb'] if system['peak_rss_mb'] is not None else 'n/a'} MB |",
            f"| calls (last 200) | {calls['n']} |",
            f"| success rate | {calls['success_rate'] if calls['success_rate'] is not None else 'n/a'} |",
            f"| p50 / p95 latency | {calls['p50_ms']} / {calls['p95_ms']} ms |",
            f"| agent_timeout | {PARAMS['agent_timeout']} s |",
        ]
        return ok(self.name, "**System metrics (measured)**\n\n| metric | value |\n|---|---|\n" + "\n".join(rows))


class DebuggerSentinelAgent(Agent):
    """Tracks every unexpected exception in SQLite. Deduplication key: agent, exception type, normalised message, and a hash
    of the innermost stack frames (file name and function name, never line numbers). The repair hook for the failing agent
    runs after each record; a failing repair never hides the original error."""

    name = "Debugger Sentinel"
    STACK_FRAMES = 6

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.healers: dict[str, Callable[[], bool]] = {}

    @classmethod
    def stack_hash(cls, exc: BaseException) -> str:
        frames = traceback.extract_tb(exc.__traceback__)[-cls.STACK_FRAMES:] if exc.__traceback__ is not None else []
        key = "|".join(f"{os.path.basename(f.filename)}:{f.name}" for f in frames)
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def signature(cls, agent: str, exc: BaseException) -> str:
        normalized = re.sub(r"0x[0-9a-fA-F]+|\d+", "N", str(exc))[:200]
        return hashlib.sha256(f"{agent}|{type(exc).__name__}|{normalized}|{cls.stack_hash(exc)}".encode()).hexdigest()[:16]

    def register_healer(self, agent: str, repair: Callable[[], bool]) -> None:
        self.healers[agent] = repair

    def record(self, agent: str, exc: BaseException) -> int:
        signature = self.signature(agent, exc)
        count = self.store.log_error(signature, agent, type(exc).__name__, str(exc), self.stack_hash(exc))
        log.warning("%s: %s (%s, seen %d times)", agent, type(exc).__name__, str(exc)[:200], count)
        repair = self.healers.get(agent)
        if repair is not None:
            try:
                if repair():
                    self.store.mark_healed(signature)
            except Exception:
                log.exception("repair hook for %s failed", agent)
        return count

    async def process(self, text: str, ctx: dict) -> AgentResult:
        errors = self.store.top_errors(limit=10)
        if not errors:
            return ok(self.name, "Koi unexpected error record nahi hua (error_log khaali hai).")
        rows = "\n".join(f"| {e['agent']} | {e['exc_type']} | {e['count']} | {'yes' if e['healed'] else 'no'} | "
                         f"{e['sample_message'][:80].replace('|', '/')} |" for e in errors)
        return ok(self.name, "**Top recorded errors (from galaxy_memory.db)**\n\n"
                             "| agent | exception | count | self-healed | sample |\n|---|---|---|---|---|\n" + rows)


SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_size(d: bytes) -> tuple[int, int]:
    i = 2
    while i + 9 < len(d):
        if d[i] != 0xFF:
            i += 1
            continue
        marker = d[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9:
            break
        if marker in SOF_MARKERS:
            height, width = struct.unpack(">HH", d[i + 5:i + 9])
            return width, height
        i += 2 + struct.unpack(">H", d[i + 2:i + 4])[0]
    raise ValueError("JPEG has no frame header")


def _webp_size(d: bytes) -> tuple[int, int]:
    chunk = d[12:16]
    if chunk == b"VP8 ":
        if d[23:26] != b"\x9d\x01\x2a":
            raise ValueError("bad VP8 signature")
        return struct.unpack("<H", d[26:28])[0] & 0x3FFF, struct.unpack("<H", d[28:30])[0] & 0x3FFF
    if chunk == b"VP8L":
        if d[20] != 0x2F:
            raise ValueError("bad VP8L signature")
        b0, b1, b2, b3 = d[21], d[22], d[23], d[24]
        return 1 + (((b1 & 0x3F) << 8) | b0), 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
    if chunk == b"VP8X":
        return 1 + int.from_bytes(d[24:27], "little"), 1 + int.from_bytes(d[27:30], "little")
    raise ValueError("unknown WEBP chunk type")


def parse_image(data: bytes) -> dict:
    """Format and structure of an image or PDF from its header and markers (see vision.py)."""
    return vision.inspect(data)


class VisionMetadataAgent(Agent):
    """Zero-dependency structural inspection of PNG, JPEG, GIF, WEBP and PDF files: header, chunks, segments and PDF
    markers. It never decodes pixels, never runs embedded code, and refuses anything that is not one of these formats."""

    name = "Vision & Document Metadata"
    MAX_BYTES = 20 * 1024 * 1024

    async def process(self, text: str, ctx: dict) -> AgentResult:
        payload = ctx.get("image_base64")
        if not payload:
            return fail(self.name, "image nahi mili - JSON me 'image_base64' field bhejein (PNG, JPEG, GIF, WEBP or PDF)")
        body = payload.split(",", 1)[1] if payload.startswith("data:") and "," in payload else payload
        if len(body) > self.MAX_BYTES * 4 // 3 + 8:
            return fail(self.name, "file is larger than 20 MB")
        try:
            raw = base64.b64decode("".join(body.split()), validate=True)  # MIME line breaks are not corruption (A20-01)
        except (binascii.Error, ValueError):
            return fail(self.name, "image_base64 is not valid base64")
        try:
            meta = vision.inspect(raw)
        except ValueError as exc:
            return fail(self.name, f"cannot read file metadata: {exc}")
        rows = [f"| {key.replace('_', ' ')} | {value} |" for key, value in meta.items()
                if key not in ("payload_bytes", "exif") and value not in (None, "")]
        exif = meta.get("exif") or {}
        rows += [f"| exif {key} | {value} |" for key, value in exif.items()]
        notes = []
        if meta.get("truncated"):
            notes.append("the file is truncated after a valid header; the header values are reliable")
        if meta.get("warning"):
            notes.append(meta["warning"])
        if meta.get("javascript") or meta.get("launch_action"):
            notes.append("PDF contains JavaScript or a launch action; it is reported, not executed")
        if meta.get("encrypted"):
            notes.append("PDF is encrypted; page counts are not reliable")
        footer = "\n\n*Structure only: this build does not decode pixels or describe content.*"
        if notes:
            footer += "\n\nNotes: " + "; ".join(notes) + "."
        table = "| field | value |\n|---|---|\n" + "\n".join(rows)
        megapixels = round(meta["width"] * meta["height"] / 1e6, 3) if "width" in meta else None
        return ok(self.name, table + footer, **{k: v for k, v in meta.items() if k != "exif"},
                  bytes=len(raw), megapixels=megapixels)
