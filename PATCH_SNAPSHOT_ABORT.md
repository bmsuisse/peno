# Patch: hardening `SnapshotBuilder` against the same SIGABRT bug class (v0.2.2)

This is a follow-up to `PATCH_LARGE_SCRIPT_ABORT.md` (v0.2.1), which fixed a
real, reproduced SIGABRT in `Runtime`'s code path. This patch closes the same
*latent* gap in `SnapshotBuilder`, but — unlike the earlier patch — the crash
does **not** reproduce there today. Read on for why, and why the fix is still
worth making.

## The shared root cause

`deno_core::runtime::setup::register_isolate` snapshots
`tokio::runtime::Handle::try_current()` at isolate-creation time. If a V8
background/delayed task is later posted for that isolate and no handle was
captured, `deno_core` calls `std::process::abort()` — uncatchable from Rust
or Python.

`SnapshotBuilder::new` (`src/runtime/snapshot.rs`, `create_runtime()`) builds
its `JsRuntimeForSnapshot` directly on whatever thread calls it from Python
(typically the main thread), with no tokio runtime ever entered there —
exactly the same precondition that caused the v0.2.1 bug in
`spawn_runtime_thread`.

## What's different here: it doesn't reproduce

`spawn_runtime_thread` reliably aborted past ~148KB of JS source. The same
technique against `SnapshotBuilder` was tried extensively and did **not**
crash:

- Plain filler scripts (`var x=0;` repeated) up to 20MB as the bootstrap
  script (100x+ past the v0.2.1 threshold).
- Heap-building scripts producing 300MB+ of serialized snapshot data.
- GC-churn scripts creating and discarding ~500MB of garbage across 50
  rounds, to force multiple major GC cycles.
- Scripts with 60,000+ top-level function declarations (to probe
  lazy-compilation/pre-parse background dispatch specifically).

None of these triggered the abort, before or after this fix.

The reason: `SnapshotBuilder` always builds with `will_snapshot = true`.
`deno_core::runtime::setup::create_isolate` branches on that flag and, when
true, creates the isolate via V8's `SnapshotCreator`
(`snapshot::create_snapshot_creator`) instead of a plain `v8::Isolate::new`.
Empirically, that path never posts the background/delayed V8 tasks
(streaming compilation, GC memory-reducer timers) that `spawn_delayed_task`
depends on to trigger the abort — snapshot creation appears to force fully
synchronous compilation for determinism, so `register_isolate`'s
`handle: None` is never actually consulted.

## The fix, applied anyway

`create_runtime()` now builds a minimal current-thread tokio runtime and
`.enter()`s it around `JsRuntimeForSnapshot::try_new(...)`:

```rust
let tokio_rt = tokio::runtime::Builder::new_current_thread()
    .build()
    .expect("failed to build tokio runtime");
let _tokio_enter = tokio_rt.enter();
JsRuntimeForSnapshot::try_new(RuntimeOptions { is_main: true, ..Default::default() })
```

This mirrors the v0.2.1 fix in `spawn_runtime_thread`, but lighter: snapshot
building is a synchronous, one-shot call with no ongoing event loop to
drive, so entering the runtime (no `block_on`, no dispatcher loop) for the
duration of isolate creation is sufficient. The runtime is dropped
immediately after `create_runtime()` returns.

This is deliberately defense-in-depth, not a fix for an observed crash: it
costs nothing, removes the one remaining `register_isolate` call site in
this codebase that could register `handle: None`, and protects against a
future V8/deno_core version that enables background compilation during
snapshotting (at which point this exact code path would start aborting the
way `spawn_runtime_thread` used to).

## Proof

- Before and after: all four large-script probes above (filler, heap-growth,
  GC-churn, many-function-declarations) succeed identically; none crashed
  either way.
- `tests/test_large_script_eval.py` gained a permanent regression suite for
  `SnapshotBuilder`: large bootstrap scripts (200KB-5MB) via both the
  constructor and `execute_script()`, plus an end-to-end check that a
  snapshot built from a large bootstrap script still works when used to
  start a real `Runtime`.
- Full existing suite passes unchanged: 332 pytest tests (was 316 pre-v0.2.1
  patch, plus this patch's own additions) and 29 `cargo test --release
  --features bench` tests (single-threaded, per the v0.2.1 patch's note
  about a pre-existing parallel-test flake unrelated to either patch).
