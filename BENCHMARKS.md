# Benchmarks

Two suites: Rust-level (Criterion, `benches/`, bypasses the Python API) and
Python-level (pytest-benchmark, `benches_py/`, the real user-facing surface).
Both run in CI on every push/PR (`.github/workflows/benchmarks.yml`),
informational only -- they report numbers, they don't gate merges.

Numbers below are one real measured run, not simulated. They are
environment-dependent -- re-run locally before trusting them for a regression
decision on different hardware.

## Measurement environment

- Apple M2, 8 cores, 16 GB RAM, macOS 25.5.0 (Darwin), arm64
- Rust 1.93.1, `cargo bench --features bench` (release/`bench` profile)
- Python 3.14.3, `pytest-benchmark` 5.3.0, `maturin develop --release`

## Rust (Criterion, `cargo bench --features bench`)

| Benchmark | Time |
|---|---|
| `isolate_creation_and_close` | 2.52 ms |
| `simple_eval_throughput` (`1 + 41`) | 4.20 µs |
| `host_callback_op_dispatch` (real Python callable via `register_op`) | 13.14 µs |
| `termination_handle/is_terminated_check` | 0.92 ns |
| `termination_handle/terminate_round_trip` (idle runtime) | 87.16 µs |
| `pooled_isolate_checkout_eval_release` (`IsolatePool`, size 4) | 167.20 µs |

`pooled_isolate_checkout_eval_release` re-measures the exact same unit of
work as `isolate_creation_and_close` above (get a ready-to-use isolate, run
one eval, give it back) but through `IsolatePool::checkout()` instead of
`RuntimeHandle::spawn()`/`close()` -- i.e. it's the direct before/after
comparison for the pooling work in this release. **3.01 ms &rarr; 167.2 µs,
~18x.** The gap is the OS thread spawn/join and V8 `Isolate` construction
that pooling amortizes away; both benches still pay for a fresh `v8::Context`
per call, since that's the whole point (see "Isolate reuse" below).

`is_terminated()` is a single atomic load -- effectively free to poll. The
206x-larger `terminate_round_trip` number is the cost of the full path:
requesting termination, calling `v8::IsolateHandle::terminate_execution()`,
and waiting for the runtime thread to acknowledge shutdown.

## Python (`pytest-benchmark`, `pytest benches_py/ --benchmark-only`)

| Benchmark | Mean |
|---|---|
| `test_cold_start` (new `Runtime()` + one eval) | 2.82 ms |
| `test_steady_state_100_evals` (100 sequential evals, warm runtime) | 363.25 µs total (~3.63 µs/eval) |
| `test_steady_state_1000_evals` (1000 sequential evals, warm runtime) | 3.46 ms total (~3.46 µs/eval) |
| `test_host_callback_round_trip` (`bind_function` + call from JS) | 13.98 µs |
| `test_normal_completing_eval_baseline` | 3.77 µs |
| `test_watchdog_termination_overhead` | 59.62 ms |
| `test_pooled_checkout_eval_release` (`IsolatePool(size=4)`, checkout+eval+release) | 184.67 µs |

`test_pooled_checkout_eval_release` is the Python-layer version of the same
before/after comparison as the Rust benches above: **`test_cold_start`
2.99 ms &rarr; `test_pooled_checkout_eval_release` 184.7 µs, ~16x**, measured
in the same run (`pytest benches_py/test_bench_runtime.py::test_cold_start
benches_py/test_bench_runtime.py::test_pooled_checkout_eval_release
--benchmark-only`). That's the real, user-facing speedup pooling buys: still
paying full isolate-creation cost is ~16-18x slower than reusing a warm one.

**Read that 16-18x against the right baseline.** It compares pooling to
*constructing a new `Runtime` per call*, not to *retaining* one. The figure
that was missing here for a long time, and whose absence invited the wrong
conclusion, is the cost of a call on a `Runtime` you simply kept:

| Operation | Cost | Hosts ops / tool calls? |
|---|---|---|
| Warm `Runtime`, full host tool call (`bind_function` → JS → host → value) | **13.4 µs** | yes |
| Warm `Runtime`, plain `eval` (`1+41`) | 3.77 µs | yes |
| `IsolatePool` checkout + eval + release | 167-185 µs | **no** |

So a retained warm `Runtime` is **~12x faster than a pool checkout** and can
host tool calls, which the pool cannot. `IsolatePool` is the fast path for
the *stateless, mutually-untrusting, one-eval-each* case, where a fresh
`Context` per call is the product requirement — it is **not** the fast path
for tool-calling workloads, where retaining one `Runtime` per session wins on
both speed and capability. See
[`docs/tool-calling-at-pool-speed.md`](docs/tool-calling-at-pool-speed.md)
for the full measurement run and the session-affinity design that follows
from it (including why a `reset()` is deliberately not offered).

Per-eval cost at the Python layer (~3.5-3.8 µs) matches the Rust-level
`simple_eval_throughput` number closely -- the Python binding adds negligible
overhead over the raw `RuntimeHandle`.

### Watchdog-termination proof, timed precisely

`test_watchdog_termination_overhead` runs the exact scenario in
`tests/test_termination_handle.py`: a runaway `while(true){}` eval, killed
from a separate watchdog thread via `TerminationHandle.terminate()`, but with
the watchdog's sleep shortened to 50 ms (20 rounds) so the suite stays fast.
Measured mean: 59.62 ms. Subtracting the artificial 50 ms delay leaves
**~9.6 ms** of real overhead for the cross-thread termination path itself
(watchdog wakes up, calls into V8's isolate handle from a foreign thread,
the runtime thread unwinds and returns control to Python) -- consistent with
the Rust-level `terminate_round_trip` number once you add Python's own
call/exception overhead on top.

## Isolate reuse (`IsolatePool`, v0.2.0)

Creating a `v8::Isolate` (~3 ms above) is the expensive part of starting a
JS runtime; a fresh `v8::Context` inside an already-live isolate is cheap
and is V8's own isolation boundary for JS-visible global state
(`globalThis`, prototypes, etc. all live on the `Context`, not the
`Isolate`). `IsolatePool` keeps a small set of isolates warm across calls
but always hands out a brand-new, empty `Context` per checkout, so no state
is JS-visible across two checkouts even when they land on the same
underlying isolate -- see `src/runtime/pool.rs` for the full reasoning and
the documented residual risks (isolate-wide compilation cache, microtask
queue) that were checked and found not to leak data. This is a narrower,
additive fast path (`eval()` only, JSON-safe values only, no ops/modules/host
bindings) that sits next to `Runtime`, not a replacement for it.

`tests/test_isolate_pool.py` and the Rust unit tests in `src/runtime/pool.rs`
both include a dedicated leakage test: check out an isolate, set
`globalThis.leaked`, release it, check out the *same* isolate again
(guaranteed via a pool of size 1), and assert `typeof leaked === 'undefined'`.

## Retained runtimes: idle cost and scaling (v0.2.0)

Until v0.2.0, `RuntimeDispatcher::run` (`src/runtime/runner.rs`) selected
between `cmd_rx.recv()` and `tokio::task::yield_now()`. `yield_now` is always
immediately ready, so the loop never actually blocked: **every live `Runtime`
burned CPU continuously for its whole lifetime, whether or not it had any work
to do.** v0.2.0 parks the dispatcher when the event loop is drained and no job
is queued, and waits on a real waker (plus the active job's exact deadline)
while async work is in flight.

This matters because retaining one warm `Runtime` per session is the fastest
thing this library does (see the warm-tool-call figures in
`docs/tool-calling-at-pool-speed.md`), and the busy-spin was what capped that
pattern at roughly the core count. Measured on the environment above, with K
retained runtimes each holding a bound host function, idle after one call:

| K retained | idle CPU, v0.1.0 | idle CPU, v0.2.0 | per-call, v0.1.0 | per-call, v0.2.0 |
|---|---|---|---|---|
| 1 | 12.9% | **0.0%** | 15.4 µs | **9.8 µs** |
| 4 | 65.3% | **0.0%** | 13.1 µs | **9.7 µs** |
| 8 | 169.9% | **0.0%** | 14.5 µs | **9.6 µs** |
| 16 | 514.3% | **0.0%** | 29.5 µs | **9.9 µs** |
| 32 | 668.3% | **0.0%** | 35.1 µs | **9.7 µs** |
| 64 | 684.1% | **0.0%** | 18.2 µs | **9.8 µs** |

Idle CPU is `getrusage(RUSAGE_SELF)` user+system over a 2 s window with every
runtime idle, as a percentage of one core (so 800% is this 8-core machine fully
saturated). Per-call is the median of the best of five 1000-call trials of a
warm `eval` that crosses into Python and back, run on one of the K live
runtimes; the earlier trials in each run are slower purely from warm-up, and
the full per-trial series is in the commit message for this change.

Three things to read off it:

- **Idle cost went from linear in K to zero.** v0.1.0 cost ~13% of a core per
  idle runtime and saturated all 8 cores somewhere between K=16 and K=32. In
  v0.2.0 idle runtimes are genuinely parked and measure 0.0% at every K.
- **Per-call latency no longer degrades with K.** v0.1.0 was flat to K=8 and
  then ~2.3x worse at K=16-32, where the spinning threads outnumbered the
  cores. v0.2.0 is flat at ~9.8 µs from K=1 to K=64. The K=64 v0.1.0 row
  reading *better* than K=32 is not a recovery: at that point the machine is
  saturated and the numbers are dominated by scheduling noise, which is the
  regime the change removes.
- **v0.2.0 is also faster at K=1** (9.8 µs vs 15.4 µs), because the old
  spinning dispatcher thread was competing with the calling Python thread even
  in the single-runtime case.

Async paths were re-measured to confirm parking costs them nothing
(medians, `eval_async`):

| Path | v0.1.0 | v0.2.0 |
|---|---|---|
| `eval_async` resolved promise | 56.3 µs | 53.6 µs |
| `eval_async` microtask chain | 55.8 µs | 47.5 µs |
| async `bind_function`, not awaited | 117.0 µs | 114.6 µs |
| async `bind_function`, awaited | 152.0 µs | 120.2 µs |

`tests/test_idle_cpu.py` is the permanent regression test for all of this. It
asserts an idle-CPU budget per runtime and that per-call latency at K = 3x the
core count stays within 2x of its own K=1 baseline; all four cases fail against
v0.1.0's dispatcher and pass against v0.2.0's.

## Known pre-existing environment flake (not caused by this pooling work)

While re-running these benchmarks, `cargo bench --features bench` and the
full `pytest benches_py/`/`pytest tests/` runs intermittently abort the whole
process with a V8-internal panic ("`V8 posted a delayed task, but this
isolate was created outside of a tokio runtime context`" or a GC-time
`SIGABRT` under heap-limit/high-concurrency scenarios). This reproduces
identically on a clean checkout of `main` with none of the `IsolatePool`
changes present, so it predates this work -- it looks like a `deno_core`
0.409.0 / `v8` 150.4.0 interaction, not something `IsolatePool` introduces.
Affected pre-existing tests: `TestRuntimeHeapLimits::test_*_eval_triggers_heap_termination`
and `TestRuntimeTimeout::test_concurrent_sync_operations_different_timeouts`
in `tests/test_runtime.py`, and the `simple_eval_throughput`/`host_callback_op_dispatch`/
`termination_handle` Criterion benches when run in the same process as
`isolate_creation_and_close`. Numbers in this document were captured by
running the unaffected benches individually
(`cargo bench --bench runtime_benches -- "isolate_creation_and_close|pooled_isolate_checkout_eval_release"`,
`pytest benches_py/test_bench_runtime.py::test_cold_start benches_py/test_bench_runtime.py::test_pooled_checkout_eval_release --benchmark-only`),
which avoids the flake and still gives a real, reproducible before/after
comparison. Investigating/fixing the underlying flake is separate follow-up
work, tracked as a known issue rather than silently worked around.

## Reproducing

```bash
cargo bench --features bench
uv sync --group all
uv run maturin develop --uv --release
uv run pytest benches_py/ --benchmark-only
```

If the full run hits the pre-existing flake above, re-run the two pooling
benches directly (see the exact commands in that section) to reproduce just
the pooling numbers.
