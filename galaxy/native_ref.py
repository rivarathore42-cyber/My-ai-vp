"""Pure-Python reference for the native ops (Phase 3).

Two roles, and only these two:
  1. Oracle in the tests: the C++ engine must return the same result on the same payload.
  2. Labelled fallback for the Zig kernel, used only when the Zig binary is absent (never when it is present and fails).
Semantics follow cpp_engine/src/main.cpp: sum, mean, sort, primes_upto (primes <= n), matrix_trace (NxN flattened).
"""
from __future__ import annotations

import math

OPS = ("sum", "mean", "sort", "primes_upto", "matrix_trace")


def sieve(n: int) -> list[int]:
    if n < 2:
        return []
    flags = bytearray([1]) * (n + 1)
    flags[0] = flags[1] = 0
    for i in range(2, math.isqrt(n) + 1):
        if flags[i]:
            flags[i * i::i] = bytearray(len(range(i * i, n + 1, i)))
    return [i for i in range(n + 1) if flags[i]]


def run(op: str, payload: list[float]):
    if op == "sum":
        return float(sum(payload))
    if op == "mean":
        if not payload:
            raise ValueError("payload is empty")
        return sum(payload) / len(payload)
    if op == "sort":
        return sorted(payload)
    if op == "primes_upto":
        if not payload:
            raise ValueError("payload must contain [n]")
        if payload[0] < 0:
            raise ValueError("n must be non-negative")
        return sieve(int(payload[0]))
    if op == "matrix_trace":
        size = math.isqrt(len(payload))
        if not payload or size * size != len(payload):
            raise ValueError("payload length must be a perfect square (NxN flattened)")
        return float(sum(payload[i * size + i] for i in range(size)))
    raise ValueError(f"op '{op}' has no Python reference")
