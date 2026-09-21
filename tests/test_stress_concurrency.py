"""Stress/load tests: many concurrent runtimes, and rapid pool churn.

The pool-churn test here is the one that matters. `IsolatePool` exists to be
held for a server process's entire lifetime, so "does memory grow without
bound under churn?" is its central correctness question -- and the answer was
*yes* until v0.3: 3000 checkout/release cycles against a pool of 4 grew peak
RSS by 515MB, because every eval gets a fresh `v8::Context` and a dropped
context is only unreachable, not freed, until a major GC that thousands of
tiny evals never trigger. See `src/runtime/pool.rs`'s module header for the
full root cause and the fix (`PoolCommand::Reclaim` on release).

RSS is measured in a **fresh subprocess** per scenario, deliberately.
`resource.getrusage` reports *peak* RSS, which is monotonic for the life of a
process -- so measuring in-process would let an earlier, unrelated test's
allocations mask a real regression and turn this into a false pass.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading

import pytest

from peno import IsolatePool, Runtime

# The plan suggested 10,000 cycles. 2,000 is used here: it is comfortably
# past the point where the regression is unambiguous (the pre-fix growth at
# 1,000 cycles was already ~208MB against a ~0.5MB post-fix figure) while
# keeping the test around a second, so it can live in the default suite
# rather than behind a marker nobody runs. Raise CYCLES locally to push it.
CYCLES = 2000

# Pre-fix growth at 2,000 cycles was ~340MB. Post-fix it is under 1MB. 50MB
# is far above the real figure and far below the regression, so this fails
# loudly on a reintroduced leak without flaking on allocator jitter.
MAX_GROWTH_MB = 50.0

_RSS_HELPER = """
import resource, sys
def rss_mb():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024
"""


def _run_in_fresh_process(body: str) -> dict[str, float]:
    """Run `body` in a clean interpreter and parse its `key=value` output."""
    script = _RSS_HELPER + textwrap.dedent(body)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, (
        f"stress child failed ({completed.returncode}):\n"
        f"stdout: {completed.stdout}\nstderr: {completed.stderr}"
    )
    out = completed.stdout.strip().splitlines()[-1]
    return {k: float(v) for k, v in (part.split("=") for part in out.split())}


class TestPoolChurnDoesNotLeak:
    def test_rapid_checkout_release_churn_does_not_grow_rss(self) -> None:
        """The regression test for the 515MB pool leak.

        Round-robins over a pool of 4, which is the *worst* case: the churn
        spreads contexts across four heaps so no single isolate ever feels
        enough allocation pressure to collect on its own.
        """
        result = _run_in_fresh_process(f"""
            from peno import IsolatePool
            pool = IsolatePool(size=4)
            for _ in range(20):                    # warm up; not measured
                with pool.checkout() as iso:
                    iso.eval("1")
            base = rss_mb()
            for _ in range({CYCLES}):
                with pool.checkout() as iso:
                    iso.eval("const o = {{a:1,b:[1,2,3]}}; JSON.stringify(o).length")
            print(f"growth={{rss_mb() - base}} base={{base}} idle={{pool.idle_count()}}")
        """)

        assert result["idle"] == 4, "pool did not return to full idle capacity"
        assert result["growth"] < MAX_GROWTH_MB, (
            f"IsolatePool leaked under churn: RSS grew {result['growth']:.1f}MB "
            f"over {CYCLES} checkout/release cycles (limit {MAX_GROWTH_MB}MB). "
            "This is the v0.2.2 dead-context leak; see src/runtime/pool.rs."
        )

    def test_churn_on_a_single_isolate_does_not_grow_rss(self) -> None:
        """Same property with pool=1, isolating "one isolate, many contexts"
        from "many isolates" -- pre-fix this grew ~131MB."""
        result = _run_in_fresh_process(f"""
            from peno import IsolatePool
            pool = IsolatePool(size=1)
            for _ in range(20):
                with pool.checkout() as iso:
                    iso.eval("1")
            base = rss_mb()
            for _ in range({CYCLES}):
                with pool.checkout() as iso:
                    iso.eval("const o = {{a:1}}; JSON.stringify(o).length")
            print(f"growth={{rss_mb() - base}} base={{base}} idle={{pool.idle_count()}}")
        """)

        assert result["idle"] == 1
        assert result["growth"] < MAX_GROWTH_MB, (
            f"single pooled isolate leaked {result['growth']:.1f}MB over "
            f"{CYCLES} cycles"
        )

    def test_churn_without_eval_is_flat(self) -> None:
        """Control: proves the checkout/release bookkeeping, worker threads
        and channels leak nothing on their own, so a failure in the tests
        above really is about contexts."""
        result = _run_in_fresh_process(f"""
            from peno import IsolatePool
            pool = IsolatePool(size=4)
            for _ in range(20):
                with pool.checkout():
                    pass
            base = rss_mb()
            for _ in range({CYCLES}):
                with pool.checkout():
                    pass
            print(f"growth={{rss_mb() - base}} base={{base}} idle={{pool.idle_count()}}")
        """)

        assert result["idle"] == 4
        assert result["growth"] < 10.0, (
            f"checkout/release bookkeeping itself leaked {result['growth']:.1f}MB"
        )


class TestPoolChurnStaysCorrect:
    """Churn must not break the pool's behavioural guarantees either."""

    def test_idle_count_returns_to_capacity_after_every_batch(self) -> None:
        pool = IsolatePool(size=4)
        for _ in range(50):
            handles = [pool.checkout() for _ in range(4)]
            assert pool.idle_count() == 0
            for handle in handles:
                handle.release()
            assert pool.idle_count() == 4

    def test_results_stay_correct_across_many_cycles(self) -> None:
        pool = IsolatePool(size=2)
        for i in range(500):
            with pool.checkout() as isolate:
                assert isolate.eval(f"{i} * 2") == i * 2

    def test_fresh_context_guarantee_holds_across_many_cycles(self) -> None:
        """The pool's core security property, under churn rather than once."""
        pool = IsolatePool(size=1)
        for i in range(200):
            with pool.checkout() as isolate:
                assert isolate.eval("typeof leaked") == "undefined", (
                    f"state leaked into cycle {i}"
                )
                isolate.eval("globalThis.leaked = 'secret'")


class TestConcurrentRuntimes:
    def test_many_runtimes_across_threads_shut_down_cleanly(self) -> None:
        """Extends `test_isolate_state_does_not_leak_between_runtimes` to the
        scale an agent server actually runs at.

        Note on what this does and does not prove: each `Runtime` pins its own
        *Rust* OS thread, which `threading.active_count()` cannot see, so the
        Python thread count only confirms the test's own workers are gone.
        Leaked runtime threads would instead show up as RSS growth, which
        `test_many_runtime_lifecycles_do_not_grow_rss` covers.
        """
        threads_before = threading.active_count()
        errors: list[BaseException] = []
        results: list[int] = []
        lock = threading.Lock()

        def worker(n: int) -> None:
            try:
                with Runtime() as rt:
                    rt.bind_function("double", lambda x: x * 2)
                    value = rt.eval(f"double({n})")
                    with lock:
                        results.append(value)
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                with lock:
                    errors.append(exc)

        workers = [threading.Thread(target=worker, args=(i,)) for i in range(32)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(timeout=120)

        assert not errors, f"concurrent runtimes raised: {errors[:3]}"
        assert sorted(results) == [i * 2 for i in range(32)]
        assert not any(t.is_alive() for t in workers), "a worker thread hung"
        assert threading.active_count() <= threads_before + 1

    def test_many_runtime_lifecycles_do_not_grow_rss(self) -> None:
        """Creating and closing many runtimes must not leak isolates or
        threads -- either would show up here."""
        result = _run_in_fresh_process("""
            from peno import Runtime
            for _ in range(10):
                with Runtime() as rt:
                    rt.eval("1")
            base = rss_mb()
            for _ in range(200):
                with Runtime() as rt:
                    rt.bind_function("f", lambda: 1)
                    rt.eval("f()")
            print(f"growth={rss_mb() - base} base={base}")
        """)

        assert result["growth"] < MAX_GROWTH_MB, (
            f"Runtime create/close cycles leaked {result['growth']:.1f}MB"
        )

    def test_a_pool_is_safe_to_share_across_threads(self) -> None:
        pool = IsolatePool(size=4)
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                for i in range(50):
                    with pool.checkout() as isolate:
                        assert isolate.eval(f"{i} + 1") == i + 1
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert not errors, f"concurrent pool use raised: {errors[:3]}"
        assert not any(t.is_alive() for t in threads)
        assert pool.idle_count() == 4


@pytest.mark.parametrize("size", [1, 2, 8])
def test_pool_of_various_sizes_returns_to_capacity(size: int) -> None:
    pool = IsolatePool(size=size)
    for _ in range(100):
        with pool.checkout() as isolate:
            isolate.eval("1")
    assert pool.idle_count() == size
