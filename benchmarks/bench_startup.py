#!/usr/bin/env python3
"""Startup benchmark: peno runtime vs subprocess overhead"""

import subprocess
import time
from peno import Runtime


def bench_peno(iterations: int = 100) -> float:
    """Measure peno runtime creation overhead"""
    start = time.perf_counter()
    for _ in range(iterations):
        with Runtime() as rt:
            rt.eval("2 + 2")
    return time.perf_counter() - start


def bench_subprocess(iterations: int = 100) -> float:
    """Measure subprocess overhead"""
    start = time.perf_counter()
    for _ in range(iterations):
        subprocess.run(
            ["node", "-e", "2 + 2"],
            capture_output=True,
            check=True,
        )
    return time.perf_counter() - start


def main() -> None:
    iterations = 100
    print(f"Comparing {iterations} JavaScript evaluations:")
    print("  • peno: create Runtime → eval → destroy")
    print("  • Node.js: spawn process → eval → terminate")
    print("-" * 60)

    # Warmup
    bench_peno(5)
    bench_subprocess(5)

    peno_time = bench_peno(iterations)
    subprocess_time = bench_subprocess(iterations)

    peno_per = (peno_time / iterations) * 1000
    subprocess_per = (subprocess_time / iterations) * 1000

    print(f"peno Runtime:      {peno_time:.3f}s total, {peno_per:.2f}ms per cycle")
    print(
        f"Node.js subprocess: {subprocess_time:.3f}s total, {subprocess_per:.2f}ms per cycle"
    )
    print("-" * 60)
    print(f"peno is {subprocess_time / peno_time:.2f}x faster")


if __name__ == "__main__":
    main()
