# Patch: safe cross-thread termination (`TerminationHandle`)

**Fork of:** https://github.com/imfing/jsrun
**Upstream base commit:** `34b786d2db410cdd264e9c8607ad7cb57e873a64` (tag-less `main`, fetched 2026-09-20)
**Original interim fork (where this fix was authored and proven):** https://github.com/dominikpeter/jsrun, branch `fix/termination-handle-unsendable-panic`
**Current home (renamed package, this repo):** https://github.com/bmsuisse/peno

## The bug

`peno`'s outer PyO3 wrapper (`src/runtime/python/runtime.rs:22`) declares:

```rust
#[pyclass(unsendable, weakref)]
pub struct Runtime { ... }
```

PyO3's `unsendable` flag makes **any** method call on a `Runtime` instance
panic (`pyo3_runtime.PanicException`: `Runtime is unsendable, but sent to
another thread`) if the call comes from a thread other than the one that
created the object -- including `terminate()`, even though `terminate()`'s
own body (`RuntimeHandle::terminate`, `src/runtime/handle.rs`) is internally
thread-safe: it bottoms out in `TerminationController`
(`src/runtime/runner.rs:122-134, 248-310`), which wraps only a
`v8::IsolateHandle` obtained via `Isolate::thread_safe_handle()` --
documented by `deno_core`/`v8` as `Clone + Send + Sync`, built specifically
for calling `terminate_execution()` from a different thread than the one
running the isolate.

Net effect: `peno`'s synchronous `Runtime.eval()` has no working
timeout/kill path from a watchdog thread. The "obvious" fix -- spawn a thread
that calls `runtime.terminate()` after N seconds -- panics instead of
stopping the runaway JS, and the runaway loop keeps running until the whole
process is force-killed externally.

## The fix

Split the PyO3 surface, per `TerminationController`'s existing (and correct)
design:

- `Runtime` stays `#[pyclass(unsendable, weakref)]` -- it has to, since it
  wraps the actual `RuntimeHandle`/V8 context, which is only safe to drive
  from its owning thread.
- A new, **separate, non-`unsendable`** pyclass, `TerminationHandle`, wraps
  only `TerminationController` (itself `Clone`, and `Send + Sync` because
  every field it owns -- `AtomicU8`, `v8::IsolateHandle`, `Mutex<Option<String>>`
  -- is `Send + Sync`). Its `.terminate()` just calls
  `IsolateHandle::terminate_execution()`, the same safe cross-thread call
  `RuntimeHandle::terminate()` already made internally -- now reachable
  without going through the `unsendable` `Runtime` object at all.
- `Runtime.termination_handle()` hands out one of these, so a caller gets a
  handle *before* starting a blocking `eval()`, then calls `.terminate()` on
  it from a watchdog thread.

### Files changed

- `src/runtime/handle.rs`: added `RuntimeHandle::termination_controller(&self) -> TerminationController`
  (a `Clone` of the field already stored on the handle; no new state).
- `src/runtime/python/runtime.rs`:
  - imported `TerminationController` from `runner`.
  - added `Runtime::termination_handle(&self) -> PyResult<TerminationHandle>`.
  - added the `TerminationHandle` pyclass (`#[pyclass(module = "_peno", frozen)]`,
    deliberately **not** `unsendable`) with `terminate()`, `is_terminated()`,
    `__repr__()`.
- `src/runtime/python/mod.rs`: re-exported `TerminationHandle`.
- `src/lib.rs`: registered `TerminationHandle` in the `_peno` pymodule.
- `python/peno/_peno.pyi`, `python/peno/__init__.py`: type stub + export
  for the new class, and a corrected docstring on `Runtime.terminate()`
  pointing callers at `termination_handle()` for the cross-thread case.

Total diff: ~90 lines including comments/stubs; the functional Rust change
(excluding docs/stubs) is under 40 lines, matching the "tens of lines" scope
identified when this bug was root-caused.

## Why this is safe

`RuntimeHandle` (and therefore `Runtime`) was never made `unsendable` because
its own state is unsafe to share -- it's `unsendable` because PyO3 requires
*some* discipline for a type that ultimately drives a `!Send` V8 isolate
in-place. `TerminationController` is a strict subset of that state that *is*
`Send + Sync` on every field, which is exactly the boundary `deno_core`
itself draws with `IsolateHandle`: it is the one thing about a V8 isolate
that is safe to touch from another thread, by design, specifically so a host
can implement watchdog-style termination.

## Proof

See `tests/test_termination_handle.py` in this fork. Summary:

- Before the fix: a watchdog thread calling `Runtime.terminate()` after 2s on
  a `while(true){}` eval panics with `pyo3_runtime.PanicException` in the
  watchdog thread; the main thread's `eval()` keeps running past the 30s
  external timeout (reproduced against upstream `34b786d2` unmodified,
  confirming the bug is present today, not a stale/fixed release).
- After the fix: `Runtime.termination_handle().terminate()` from the
  watchdog thread raises no panic, the runaway loop's `eval()` call on the
  main thread returns/raises within ~2-3s, and `is_terminated()` reports
  `True` afterward. Confirmed clean across 5 repeated runs (no flakiness).

## Re-syncing with upstream

This fork tracks the base commit noted at the top of this file.
To re-sync: `git fetch upstream && git rebase upstream/main` on the
`fix/termination-handle-unsendable-panic` branch, re-resolve the five files
above if upstream has since touched `Runtime`'s pyclass surface, and re-run
`tests/test_termination_handle.py`.

## Standalone / extraction note

This directory is a full clone of the upstream project (its own `Cargo.toml`,
`pyproject.toml`, `Cargo.lock`, `uv.lock`) with the patch applied on top --
it is not wired into any other project's build. It can be copied
(`cp -r` / `git subtree split`) directly into a new standalone repository
with no further changes.
