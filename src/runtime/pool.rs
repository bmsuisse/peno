//! `IsolatePool`: reuse warm V8 isolates across logically separate `eval` calls.
//!
//! ## Reuse boundary (read before touching this file)
//!
//! An `Isolate` (`v8::OwnedIsolate`) is expensive to create (~2.5ms measured
//! in `BENCHMARKS.md`, dominated by V8 heap setup) but a `Context` inside an
//! already-live isolate is cheap. A V8 `Context` is V8's own isolation
//! boundary for JS-visible global state: `globalThis`, the prototype chain
//! roots, and any bindings installed on the global object all live on the
//! `Context`, not the `Isolate`. Two different `Context`s in the same
//! `Isolate` never share a global object -- this is exactly what lets a
//! browser run multiple unrelated tabs/iframes in one renderer process. See
//! the V8 embedder guide's definition of a context and `v8::Context::New`.
//!
//! So the safe reuse boundary this pool implements is: **keep the `Isolate`
//! warm across calls, but give every checkout a brand-new, empty `Context`,
//! and throw that `Context` away (never reuse it) when the checkout ends.**
//!
//! ## When this pool is *not* the fast path
//!
//! The pool's headline "16-18x" is measured against *constructing* a
//! `Runtime` per call, not against *retaining* one. A retained warm
//! `Runtime` does a full host tool call in ~13.4 µs versus ~167-185 µs for a
//! checkout here, i.e. ~12x faster, and it can host ops while this pool
//! cannot. So `IsolatePool` is the fast path for the *stateless,
//! mutually-untrusting, one-eval-each* case, where a fresh `Context` per call
//! is the product requirement -- not for tool-calling workloads, which should
//! retain one `Runtime` per session instead. See
//! `docs/tool-calling-at-pool-speed.md`.
//!
//! What is *not* safe to reuse, and this pool does not attempt to:
//! - `deno_core::JsRuntime` itself. `JsRuntime` binds ops, the module loader,
//!   and embedder callbacks to one specific main `Context`
//!   (`deno_core`'s `JsRealm` type that would let a runtime host multiple
//!   contexts is a private, `pub(crate)` type in `deno_core` 0.409 -- not
//!   reachable from outside the crate). Bypassing that binding by hand to
//!   swap `JsRuntime`'s context would mean re-deriving `deno_core`'s internal
//!   realm bookkeeping ourselves, which is exactly the kind of "fast but
//!   leaky" shortcut the design brief says not to ship. So this pool does
//!   *not* wrap `JsRuntime` -- it drives a bare `v8::Isolate` directly and
//!   only supports plain synchronous `eval` (no ops, no modules, no host
//!   bindings, no persisted JS state across a release/checkout pair). Reach
//!   for the full `Runtime` (`src/runtime/handle.rs`) when a call needs those.
//!   ponytail: fast path intentionally narrow (eval-only); widen if callers
//!   need ops/streams here too -- that's real, separate effort, not a
//!   half-built version of `Runtime`.
//! - The isolate itself can never be sent across threads mid-use (`v8::OwnedIsolate`
//!   is not `Send` -- verified against `v8` 150.4.0's source, which only marks
//!   the *termination handle* type `Send + Sync`, deliberately, the same
//!   asymmetry `TerminationHandle` already exploits elsewhere in this crate).
//!   So each pooled isolate is pinned to one dedicated OS thread for its
//!   entire life, exactly like the existing per-`Runtime` thread in
//!   `runner.rs`; "checkout" routes an eval request to that thread over a
//!   channel, it never moves the isolate itself.
//!
//! ## Why release() reclaims (do not remove `PoolCommand::Reclaim`)
//!
//! Handing every checkout a brand-new `Context` is what makes reuse safe, but
//! a dropped `Context` is only *unreachable*, not freed: V8 reclaims it on a
//! major GC, and a workload of many tiny evals never allocates enough to
//! trigger one. Each isolate's heap therefore grows by roughly the size of
//! every context it has ever entered. Measured on this file's own fast path,
//! 3000 checkout/release cycles:
//!
//! | scenario                                   | RSS growth |
//! |--------------------------------------------|------------|
//! | 3000 cycles, no eval at all                |    0.0 MB  |
//! | 3000 evals on one isolate                  |  130.4 MB  |
//! | 3000 cycles round-robin over a pool of 4   |  515.2 MB  |
//! | 3000 cycles over a pool of 4, with reclaim |   36.9 MB  |
//!
//! Note the pool of 4 is ~4x *worse* than a single isolate, not better: the
//! churn spreads the same 3000 contexts across four heaps, so no individual
//! isolate ever feels enough allocation pressure to collect, and each one
//! independently grows to ~130 MB. The checkout/release bookkeeping itself
//! leaks nothing (the 0.0 MB row) -- the garbage is entirely dead contexts.
//!
//! So `release()` sends `PoolCommand::Reclaim`, and the worker answers it with
//! `Isolate::low_memory_notification()` -- V8's documented "free what you can"
//! signal, which performs a full GC. It runs on the worker's own thread after
//! the isolate is idle and before any later eval reaches it, so it never
//! interrupts running JS and never blocks the releasing caller.
//!
//! Documented residual risk (checked, not a leak): V8's isolate-level script
//! compilation cache may reuse compiled bytecode for identical source text
//! across contexts. That cache is keyed purely off source bytes, not off any
//! prior execution's *data* -- it cannot carry a value from one context's
//! `globalThis` into another's. The microtask queue is isolate-wide, so this
//! pool calls `perform_microtask_checkpoint()` before a context is dropped,
//! draining anything still queued while that context was live -- nothing is
//! left to fire later against a different context.

use crate::runtime::error::{RuntimeError, RuntimeResult};
use crate::runtime::js_value::JSValue;
use deno_core::{serde_v8, v8, JsRuntime};
use std::collections::VecDeque;
use std::sync::mpsc as std_mpsc;
use std::sync::{Arc, Mutex};
use std::thread;

enum PoolCommand {
    Eval {
        code: String,
        reply: std_mpsc::Sender<RuntimeResult<JSValue>>,
    },
    /// Free the heap left behind by the contexts this isolate has run.
    /// Sent on release; see the module docs on why this is load-bearing.
    Reclaim,
    Shutdown,
}

/// One pre-warmed OS thread pinning a single, long-lived `v8::Isolate`.
///
/// Cheaply `Clone`-able: cloning just clones the channel sender, it does not
/// duplicate the isolate or the thread.
#[derive(Clone)]
struct Worker {
    tx: std_mpsc::Sender<PoolCommand>,
}

impl Worker {
    fn spawn() -> Self {
        let (tx, rx) = std_mpsc::channel::<PoolCommand>();
        thread::Builder::new()
            .name("peno-isolate-pool-worker".to_string())
            .spawn(move || worker_loop(rx))
            .expect("failed to spawn isolate pool worker thread");
        Worker { tx }
    }

    fn eval(&self, code: String) -> RuntimeResult<JSValue> {
        let (reply_tx, reply_rx) = std_mpsc::channel();
        self.tx
            .send(PoolCommand::Eval {
                code,
                reply: reply_tx,
            })
            .map_err(|_| RuntimeError::internal("Isolate pool worker is no longer running"))?;
        reply_rx
            .recv()
            .map_err(|_| RuntimeError::internal("Isolate pool worker died mid-eval"))?
    }

    /// Ask this worker to collect its isolate's garbage.
    ///
    /// Fire-and-forget on purpose: the releasing caller should not wait for a
    /// GC. Channel ordering guarantees the worker runs it before any later
    /// `Eval`, so no checkout ever observes the un-collected heap.
    fn reclaim(&self) {
        let _ = self.tx.send(PoolCommand::Reclaim);
    }

    fn shutdown(&self) {
        let _ = self.tx.send(PoolCommand::Shutdown);
    }
}

fn worker_loop(rx: std_mpsc::Receiver<PoolCommand>) {
    // Idempotent: guarded internally by deno_core's own `Once`. Safe to call
    // from every worker thread and safe to call alongside `Runtime`'s own
    // `JsRuntime::new` calls elsewhere in the process.
    JsRuntime::init_platform(None);
    let mut isolate = v8::Isolate::new(v8::CreateParams::default());
    while let Ok(cmd) = rx.recv() {
        match cmd {
            PoolCommand::Eval { code, reply } => {
                let result = eval_in_fresh_context(&mut isolate, &code);
                let _ = reply.send(result);
            }
            PoolCommand::Reclaim => {
                // V8's "free what you can" signal: performs a full GC, which
                // is what actually releases the dropped contexts. Safe here --
                // this thread owns the isolate and no JS is running on it.
                isolate.low_memory_notification();
            }
            PoolCommand::Shutdown => break,
        }
    }
    // `isolate` drops here on this same thread, disposing the V8 isolate.
}

/// Run `code` to completion in a brand-new `Context` on `isolate`, then
/// discard that `Context`. No JS-visible state from this call is reachable
/// from a later call against the same isolate.
fn eval_in_fresh_context(isolate: &mut v8::OwnedIsolate, code: &str) -> RuntimeResult<JSValue> {
    v8::scope!(scope, isolate);
    let context = v8::Context::new(scope, Default::default());
    let scope = &mut v8::ContextScope::new(scope, context);
    v8::tc_scope!(scope, scope);

    let source = v8::String::new(scope, code)
        .ok_or_else(|| RuntimeError::internal("Failed to allocate source string"))?;

    let script_result =
        v8::Script::compile(scope, source, None).and_then(|script| script.run(scope));

    let js_value = match script_result {
        Some(value) => serde_v8::from_v8::<JSValue>(scope, value).map_err(|err| {
            RuntimeError::internal(format!(
                "Result could not be converted (pooled eval only supports JSON-safe values): {err}"
            ))
        })?,
        None => {
            let message = scope
                .message()
                .map(|m| m.get(scope).to_rust_string_lossy(scope))
                .or_else(|| scope.exception().map(|exc| exc.to_rust_string_lossy(scope)))
                .unwrap_or_else(|| "Unknown JavaScript error".to_string());
            return Err(RuntimeError::internal(message));
        }
    };

    // Drain microtasks queued while *this* context was entered before it's
    // torn down -- see module docs on the isolate-wide microtask queue.
    scope.perform_microtask_checkpoint();

    Ok(js_value)
}

struct PoolInner {
    idle: Mutex<VecDeque<Worker>>,
    max_idle: usize,
}

/// A small pool of pre-warmed V8 isolates.
///
/// `checkout()` hands out a [`PooledIsolate`] bound to one warm isolate's
/// dedicated thread; every checkout starts in a fresh, empty `Context`.
/// Returning it (`release()`, or simply dropping it) puts the isolate back
/// in the idle queue for reuse, up to `max_idle` -- isolates checked out
/// while the pool is exhausted are spun up on demand (never blocks) and are
/// torn down instead of queued if the pool is already full when returned.
pub struct IsolatePool {
    inner: Arc<PoolInner>,
}

impl IsolatePool {
    pub fn new(size: usize) -> Self {
        let size = size.max(1);
        let mut idle = VecDeque::with_capacity(size);
        for _ in 0..size {
            idle.push_back(Worker::spawn());
        }
        Self {
            inner: Arc::new(PoolInner {
                idle: Mutex::new(idle),
                max_idle: size,
            }),
        }
    }

    /// Number of pre-warmed isolates currently idle in the pool.
    pub fn idle_count(&self) -> usize {
        self.inner.idle.lock().unwrap().len()
    }

    /// Check out a warm isolate, or spin up a brand-new one if the pool is
    /// currently exhausted. Never blocks.
    pub fn checkout(&self) -> PooledIsolate {
        let worker = self
            .inner
            .idle
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or_else(Worker::spawn);
        PooledIsolate {
            worker: Some(worker),
            inner: self.inner.clone(),
        }
    }

    /// Shut down every currently-idle worker thread. Isolates already
    /// checked out finish their own lifecycle independently (they shut
    /// themselves down instead of re-queueing once the pool reports full).
    pub fn close(&self) {
        let mut idle = self.inner.idle.lock().unwrap();
        for worker in idle.drain(..) {
            worker.shutdown();
        }
    }
}

impl Drop for IsolatePool {
    fn drop(&mut self) {
        self.close();
    }
}

/// One checked-out isolate. Call [`eval`](Self::eval) any number of times
/// (each call still gets a fresh `Context` -- see module docs), then
/// [`release`](Self::release) it back to the pool, or just drop it.
pub struct PooledIsolate {
    worker: Option<Worker>,
    inner: Arc<PoolInner>,
}

impl PooledIsolate {
    pub fn eval(&self, code: &str) -> RuntimeResult<JSValue> {
        let worker = self
            .worker
            .as_ref()
            .ok_or_else(|| RuntimeError::internal("PooledIsolate has already been released"))?;
        worker.eval(code.to_string())
    }

    /// Return this isolate to the pool for reuse (or shut it down if the
    /// pool is already at capacity). Idempotent.
    pub fn release(&mut self) {
        if let Some(worker) = self.worker.take() {
            let mut idle = self.inner.idle.lock().unwrap();
            if idle.len() < self.inner.max_idle {
                // Reclaim before re-queueing: an isolate going back into the
                // pool is about to sit idle, which is the cheapest possible
                // moment to pay for a GC, and without this its heap keeps
                // every context it has ever run (see module docs).
                worker.reclaim();
                idle.push_back(worker);
            } else {
                worker.shutdown();
            }
        }
    }
}

impl Drop for PooledIsolate {
    fn drop(&mut self) {
        self.release();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn eval_returns_expected_value() {
        let pool = IsolatePool::new(1);
        let handle = pool.checkout();
        let result = handle.eval("1 + 41").unwrap();
        assert!(matches!(result, JSValue::Int(42)));
    }

    #[test]
    fn checkout_reuses_isolate_after_release() {
        let pool = IsolatePool::new(1);
        assert_eq!(pool.idle_count(), 1);
        let handle = pool.checkout();
        assert_eq!(pool.idle_count(), 0);
        drop(handle);
        assert_eq!(pool.idle_count(), 1);
    }

    /// The core security property: a fresh `Context` per checkout means no
    /// JS-visible global state survives from one logical caller to the next,
    /// even when the underlying `Isolate` (the expensive part) is reused.
    #[test]
    fn global_state_does_not_leak_across_checkouts_of_the_same_isolate() {
        let pool = IsolatePool::new(1); // size 1: guarantees the same isolate comes back.

        let mut first = pool.checkout();
        first
            .eval("globalThis.leaked = 'secret'; globalThis.leaked")
            .unwrap();
        first.release();

        let second = pool.checkout();
        let leaked_type = second.eval("typeof leaked").unwrap();
        assert!(matches!(leaked_type, JSValue::String(s) if s == "undefined"));
    }

    #[test]
    fn pool_falls_back_to_a_fresh_isolate_when_exhausted() {
        let pool = IsolatePool::new(1);
        let _first = pool.checkout(); // holds the only pre-warmed isolate
        assert_eq!(pool.idle_count(), 0);
        // Must not block waiting for `_first` to be released.
        let second = pool.checkout();
        assert_eq!(second.eval("2 + 2").unwrap(), JSValue::Int(4));
    }

    /// Reclaim must be transparent: an isolate that has been GC'd on release
    /// still works, and still starts from a clean context.
    #[test]
    fn isolate_still_works_after_a_reclaim_on_release() {
        let pool = IsolatePool::new(1); // size 1: same isolate comes back.

        for i in 0..5 {
            let mut handle = pool.checkout();
            assert_eq!(
                handle.eval("globalThis.n = 1; globalThis.n + 1").unwrap(),
                JSValue::Int(2)
            );
            handle.release();
            assert_eq!(pool.idle_count(), 1, "iteration {i}");
        }

        // And the fresh-context guarantee still holds after a GC cycle.
        let handle = pool.checkout();
        assert!(matches!(
            handle.eval("typeof n").unwrap(),
            JSValue::String(s) if s == "undefined"
        ));
    }

    #[test]
    fn javascript_errors_surface_as_runtime_errors() {
        let pool = IsolatePool::new(1);
        let handle = pool.checkout();
        let err = handle.eval("throw new Error('boom')").unwrap_err();
        assert!(matches!(err, RuntimeError::Internal { .. }));
    }
}
