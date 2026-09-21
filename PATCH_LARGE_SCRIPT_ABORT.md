# Patch: uncatchable SIGABRT on large JS source text (v0.2.1)

## The bug

Evaluating any JS source text past roughly 148KB (V8/platform-dependent; a
plain filler script of many `var x=0;` lines reproduces it, no complex JS
needed) reliably crashed the whole Python process with `SIGABRT`:

```
V8 posted a delayed task, but this isolate was created outside of a tokio
runtime context and the delay cannot be honored. Enter a tokio runtime
(e.g. via `tokio::runtime::Runtime::enter()`) before creating the
`JsRuntime`.
```

Reproduced identically via `Runtime.eval` (sync), `Runtime.eval_async`,
module-level `peno.eval_async`, `RuntimeConfig(bootstrap=...)`, and
(independently) by `test_runtime_terminates_when_heap_limit_exceeded`'s
tight heap-limit config -- the fault is source-size-or-GC-pressure
triggered, not path-specific. Unlike a hang (catchable by
`TerminationHandle`/timeout), this is a process-level abort: nothing in the
embedding Python process can catch it.

## Root cause

`deno_core` (`runtime/setup.rs`, `register_isolate`/`spawn_delayed_task`)
registers every `v8::Isolate` with a `tokio::runtime::Handle::try_current()`
snapshot at `JsRuntime::new` time. If V8 later schedules a background
foreground-task (streaming/background compilation for a large script, or a
GC memory-reducer task under heap pressure) and posts it as a *delayed*
task, `deno_core` looks up that stored handle. No handle -> it prints the
message above and calls `std::process::abort()` directly (not a panic --
this is invoked from a C++/V8 frame Rust can't unwind through).

`spawn_runtime_thread` (`src/runtime/runner.rs`) built its
`tokio::runtime::Runtime` and then called `RuntimeCoreState::new(config)`
(which owns the call to `JsRuntime::new`) **before** ever calling
`tokio_rt.block_on(...)` -- i.e. with no tokio runtime entered on that
thread yet. `Handle::try_current()` therefore always returned `Err`, so
every isolate on that thread was silently registered with `handle: None`,
one delayed task away from an abort.

## The fix

`src/runtime/runner.rs`, `spawn_runtime_thread`: enter the thread's own
tokio runtime (`let _tokio_enter = tokio_rt.enter();`) around the
`RuntimeCoreState::new(config)` call, so `Handle::try_current()` succeeds
at isolate-registration time -- exactly what `deno_core`'s own abort
message recommends. The guard is dropped before `tokio_rt.block_on(...)`
takes over driving the runtime.

## Proof

- Before: binary search on a fresh clone found the crash threshold at
  147,654 bytes of filler JS (147,303 bytes still succeeded), reproduced
  via `pip install peno` (PyPI v0.1.0) and via a from-source build of
  this repo's `main`. `test_runtime_terminates_when_heap_limit_exceeded`
  also aborted the same way pre-fix (V8 background GC tasks under tight
  heap pressure hit the identical code path).
- After: 200KB-5MB scripts evaluate correctly across all four call paths
  above; `tests/test_large_script_eval.py` pins this at 200KB-5MB plus the
  exact pre/post-threshold sizes found during triage, including one case
  that calls a bound host function from inside a large script (not just
  inert filler).
- Full existing suite (316 pytest tests including
  `tests/test_security_audit.py` and `tests/test_termination_handle.py`,
  plus 29 `cargo test --features bench` tests) passes unchanged after the
  fix.

## Incidental fix: heap-limit test's own too-tight config

`test_runtime_terminates_when_heap_limit_exceeded` used a 1MB
initial/5MB max heap, too small for the current `deno_core` version's own
extension bootstrap -- it was crashing on isolate *construction*
(unrelated to the JS under test) regardless of this patch. Bumped to
4MB/10MB and switched the trigger script from one exponentially-doubling
string (whose single oversized allocation can throw V8's own "Invalid
string length" `RangeError` before the termination interrupt is checked)
to many small incremental allocations, which reliably let the near-heap-limit
callback's `terminate_execution()` win the race.
