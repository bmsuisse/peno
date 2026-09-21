# Tool-calling at pool speed: session-affine warm runtimes

Design only, no code changed. Grounded in a full read of `src/runtime/`
(`pool.rs`, `runner.rs`, `ops.rs`, `snapshot.rs`, `config.rs`,
`python/runtime.rs`) on `main`, plus a fresh measurement run whose numbers
are tabulated below. Line numbers refer to the files as read on `main` at
2026-09-21.

## Summary

The brief that motivated this document asked for "tool-calling at near-pool
speed", on the premise that `IsolatePool` is the fast path and the ops path
has to catch up to it. **That premise is wrong, and measurement inverts it.**

A warm `Runtime` executes a *complete* host tool call — JS calls a bound
function, the op crosses into the host, a value comes back — in **13.4 µs**.
An `IsolatePool` checkout, which does one `eval` and **cannot host a tool
call at all**, costs **167–185 µs** (`BENCHMARKS.md`). Reusing a warm
`Runtime` is not "near" pool speed; it is **~12x faster than pool speed**,
and it has hosted ops the whole time.

The pool's advertised "16–18x" is real but is a comparison against
*constructing a new runtime per call* (~3 ms), not against *retaining* one.
Nobody ever benchmarked "keep the `Runtime` and call it again", which is the
cheapest thing in the entire system. Once you do, the architecture question
dissolves:

- **Pooling was never the fast path for the ops case.** It is the fast path
  for the *stateless, mutually-untrusting, one-eval-each* case, where a
  fresh `Context` per call is the product requirement. Tool-calling
  executions are the opposite: they belong to one session, one user, one
  trust boundary, and they *want* continuity.
- **`deno_core`'s private `JsRealm` is therefore not blocking anything we
  want.** `JsRealm` multiplexing would buy fresh-`Context`-per-call *with*
  ops — i.e. ~167 µs and no continuity. That is 12x slower than simply
  keeping the session's runtime. The API wall is real; it is also guarding a
  door we have no reason to walk through. See
  [The `deno_core` fork, priced](#the-deno_core-fork-priced).
- **Snapshots are a genuine second lever, and better than expected.** A
  post-bootstrap snapshot cuts cold start from 3.15 ms to **1.25 ms**, and
  ops **do** survive snapshotting (measured, see
  [Snapshots](#snapshots-the-other-lever)). That halves the only cost
  session affinity actually has: the first call in a session.

So the recommendation is the cheap one. **Retain one `Runtime` per session,
keyed by session id, with LRU + idle-TTL eviction. Do not reset it. Do not
pool it. Do not fork `deno_core`.** The expensive-looking parts of the
problem were artifacts of measuring the wrong baseline.

> **Update, peno 0.2.0.** As first written, this document had one load-bearing
> error: it assumed a retained `Runtime`'s thread was *parked* when idle. It
> was not — the dispatcher busy-spun, costing ~13% of a core per idle runtime
> and saturating an 8-core machine by K=64, which capped the very pattern
> recommended here at roughly the core count. 0.2.0 makes the dispatcher park,
> measured at 0.0% idle CPU and flat ~9.8 µs per-call latency through K=64. The
> recommendation below is unchanged; see
> [the corrected thread-cost discussion](#where-the-security-boundary-belongs)
> and `BENCHMARKS.md` for the numbers.

> **Update, peno 0.2.1.** Every tool-call figure in this document was measured
> **without `timeout=`**, and through 0.2.0 that was the only configuration in
> which they held: arming *any* deadline added a fixed ~13 ms, because the
> timeout watchdog polled for its own cancellation behind a 10 ms sleep while
> the runtime thread joined it. Since a production caller must arm a timeout —
> it is the only kill switch for runaway guest code — the 13.4 µs below was
> only ever reachable unsafely. 0.2.1 signals the cancel instead of polling
> for it; the armed path is now ~1.2x the unarmed one, so these numbers now
> describe the configuration you should actually deploy. The recommendation
> below is unchanged; see
> [Arming a timeout](../BENCHMARKS.md#arming-a-timeout-v021).

## Measurements

Apple M2, 8 cores, 16 GB, macOS 25.5.0 arm64; Python 3.12; release build
(`maturin build --release`) of the tool-bridge branch. Medians over 15–2000
iterations depending on the operation. These are one real run, not
simulated, and are environment-dependent in the same way `BENCHMARKS.md`
warns about. Rows marked *(BENCHMARKS.md)* are pre-existing repo numbers
quoted for comparison, measured on the same machine class.

| Operation | Cost | Hosts ops? |
|---|---|---|
| **Warm `Runtime`, full tool call** (`bind_function` → JS → host → value) | **13.4 µs** | yes |
| Warm `Runtime`, tool call, snapshot-started | 11.0 µs | yes |
| Warm `Runtime`, plain `eval` (`1+41`) | 3.77 µs | yes |
| `IsolatePool` checkout + eval + release *(BENCHMARKS.md)* | 167–185 µs | **no** |
| Cold `Runtime()` + one eval | 3.15 ms | yes |
| Cold `Runtime()` + `bind_function` + one tool call | 3.14 ms | yes |
| Cold `Runtime(snapshot=…)` + one eval | **1.25 ms** | yes |
| Cold `Runtime(bootstrap=<same src>)` + one eval | 3.04 ms | yes |
| Cold, heavy 200 KB bootstrap: **snapshot** | 2.27 ms | yes |
| Cold, heavy 200 KB bootstrap: **from source** | 7.06 ms | yes |
| `SnapshotBuilder(...).build()`, one-off | 15.3 ms (tiny) / 38.6 ms (heavy) | n/a |
| Snapshot size | 721 KB (tiny) / 1.54 MB (heavy) | n/a |

Retained-runtime memory, measured as process RSS high-water with K runtimes
alive, each with a bound tool and one call executed:

| K retained | plain `Runtime` | snapshot-started |
|---|---|---|
| 1 | +0.0 MB | +0.4 MB |
| 4 | +7.3 MB (1.82 MB/session) | +7.1 MB (1.77 MB/session) |
| 16 | +42.8 MB (2.68 MB/session) | +35.1 MB (2.19 MB/session) |
| 64 | +186.0 MB (2.91 MB/session) | +149.2 MB (2.33 MB/session) |

Caveat on method, stated because it changes how the curve should be read:
this is `ru_maxrss`, a **high-water mark**, so it is monotonic and cannot
show reclamation. It is the right shape for "what does holding K runtimes
cost" and the wrong tool for "does eviction give memory back" — that second
question is untested and is called out as a deferral below. The K=1 row
reading +0.0 MB is an artifact of the baseline already containing a
created-and-closed runtime's high-water, not evidence that the first runtime
is free.

**Per-session cost, in one line:** a session with M tool calls costs
`3.14 ms + M × 13.4 µs` (or `1.25 ms + M × 11.0 µs` snapshot-started),
against `M × 3.14 ms` for construct-per-call. At M=100 that is 4.5 ms versus
314 ms — **70x** — and the amortisation is already better than the pool's at
M=1.

### What the measurements changed

Two things, both of which moved the design:

1. **The 13.4 µs warm tool call.** Before measuring, the plan was going to be
   "get as close to 167 µs as possible". The real number is an order of
   magnitude *past* the target, which is what retires pooling, `JsRealm`, and
   the fork from consideration rather than merely deprioritising them.
2. **Snapshots recovering 60% of cold start, with ops intact.** Reasoning
   from `BENCHMARKS.md` alone predicted snapshots would be nearly useless:
   `isolate_creation_and_close` is 2.52 ms of a 2.82 ms cold start, so
   "everything that isn't isolate construction" looked like ≤0.3 ms of
   headroom. That reasoning was wrong, and measurement corrected it —
   3.15 ms → 1.25 ms. The 2.52 ms figure evidently includes `JsRuntime`
   bootstrap work that a snapshot *does* elide, not just raw
   `v8::Isolate` allocation. Recording the wrong prediction here
   deliberately: the cost-breakdown inference was plausible and false, and
   the next person reasoning from those two numbers will make the same
   mistake.

### Relationship to the in-flight measurement work

A separate measurement effort was quantifying the same four candidate paths
(warm reuse, global-state reset, snapshot startup, per-session affinity with
an RSS curve) while this was written. Its report was not available at the
time of writing; the numbers above are this document's own and stand on
their own. Where the two disagree, prefer whichever run is reproducible on
the reader's hardware, and treat the *ratios* (warm-vs-pool ≈ 12x,
snapshot-vs-cold ≈ 2.5x, ~2–3 MB per retained runtime) as the durable
claims rather than the absolute microsecond figures.

## Where the security boundary belongs

This is the reframing that does most of the work, so it is worth stating
precisely rather than gesturing at.

In the consuming shape this is designed for — a host application with
per-user sessions, where a session is a conversation and tool-calling
executions happen inside it — the trust boundary is **the session**, not the
individual execution. Within one session:

- Every execution is the same user's code, run on that user's behalf, with
  that user's tools bound.
- Earlier executions in the session *are* that user's own earlier code.
  State surviving from call 1 to call 2 is not a leak; for an agent building
  up a computation across turns it is frequently the desired behaviour.
- The host already has to treat the whole session as one unit for
  authorization anyway — the tools bound into the runtime carry that user's
  permissions.

Across sessions, the boundary is hard: a runtime that served session A must
never serve session B. Different users, different tool permissions,
different data.

So the correct architecture is **affinity, not pooling**: a map from session
id to a warm `Runtime`, created on the session's first tool call and
destroyed when the session ends or is evicted. A fresh `Runtime` is V8's own
isolation boundary done properly — new `Isolate`, new `Context`, new op
registry — and costs 3.14 ms once per session, or 1.25 ms with a snapshot.

Honestly assessed, this is the answer, and its costs are:

| Cost | Size | Mitigation |
|---|---|---|
| Memory per retained session | ~2.3–2.9 MB | K is bounded by LRU; 64 sessions ≈ 150–190 MB |
| OS thread per retained session | 1 (each `Runtime` pins its own thread, `runner.rs:1945`+) | Genuinely parked as of 0.2.0 (0.0% idle CPU at every K measured); same LRU bound as memory |
| Cold-start penalty on a session's first call | 3.14 ms → 1.25 ms with a snapshot | Increment 3 below |
| Cold-start penalty again after eviction | same | Tune TTL to session-idle reality; a miss is 3.14 ms, not an error |
| Cross-session leakage risk | **none by construction** | Never share a `Runtime` across session ids |

The thread-per-runtime cost deserves emphasis because it is the term that
actually binds. `Runtime` is not a lightweight object: it owns a dedicated
OS thread for the life of the isolate (the same pinning `pool.rs` documents
for pooled isolates, for the same `!Send` reason). K=64 retained sessions
means 64 parked threads. That is fine; K=10,000 is not. Any host expecting
very high session counts with very sparse activity should pick a small K and
accept cold starts on miss, which is precisely what an LRU gives.

**This paragraph was wrong when first written, in the way that mattered most,
and the correction is the reason peno 0.2.0 exists.** It asserted 64 *parked*
threads. They were not parked. `RuntimeDispatcher::run` selected between
`cmd_rx.recv()` and `tokio::task::yield_now()`, and because `yield_now` is
always immediately ready the loop never blocked — so each retained `Runtime`
burned CPU continuously for its entire lifetime whether or not it had work.
Measured on the same 8-core machine as everything else here: **12.9% of a core
per idle runtime, 169.9% at K=8, and 684% — the machine saturated — by K=64.**
Worse, per-call latency degraded ~2.3x (14.5 µs → 29.5-35 µs) once the spinning
threads outnumbered the cores, so the session-affinity recommendation this
document makes was in practice capped at roughly *cores-minus-one* concurrent
sessions, not by memory or by thread count at all.

**Fixed in 0.2.0.** The dispatcher now parks on `cmd_rx.recv()` when the event
loop is drained and no job is queued or active, and while async work *is* in
flight it waits on a real waker (`deno_core` signals it on op/promise progress,
replacing a `noop_waker` that discarded every wake) bounded by the active job's
exact deadline. Re-measured: **0.0% idle CPU at K = 1, 4, 8, 16, 32 and 64, and
per-call latency flat at ~9.8 µs across that whole range** — faster even at
K=1, since the spinning thread had been competing with the calling Python
thread. Full before/after tables, including the async paths, are in
`BENCHMARKS.md`; `tests/test_idle_cpu.py` is the permanent regression test.

So the scaling ceiling for session affinity is now what this document always
claimed it was — thread count and memory, bounded by an LRU — rather than the
core count. The recommendation stands unchanged; it just actually works now.

### Termination is now bounded for every stuck shape

Parking the dispatcher exposed a second, older hole worth stating here, because
a session-affinity host is exactly the thing that needs to trust it. A retained
`Runtime` has to be killable on demand, and `TerminationHandle.terminate()` —
the only kill switch callable from a watchdog thread — could not kill a runtime
parked on a pending promise. It flips a flag and calls V8's
`terminate_execution()`, and V8 only acts on that when it next *enters*
JavaScript; a drained-but-pending event loop never does. Nothing in the
dispatcher read the flag, so `new Promise(() => {})` was unkillable and the
caller blocked for the life of the process. This was measured identically
against 0.1.0's busy-spin loop, so it was never a parking regression — just a
gap that parking made impossible to keep ignoring.

**Also fixed in 0.2.0.** The dispatcher checks the termination flag between
polls, which kills all three parked shapes in **~1.7–2.1 ms median** (worst case
3.4 ms) instead of never. `while(true){}` still dies by V8 unwinding JS directly
at ~0.12 ms and does not pay for the poll interval, and a politely killed
runtime keeps its bound host functions and globals — a `timeout=` never costs
you your runtime. `timeout=` itself was already honest on parked promises and is
unchanged at ~302 ms for a 300 ms limit.

The one case no polite tier can reach is a runtime whose *thread* is wedged
inside a host callback that never returns. `RuntimeConfig(force_kill_grace=...)`
bounds that too, raising `RuntimeForceKilled` and abandoning the thread, but it
is opt-in: it costs ~10% per synchronous call, and it cannot reclaim the isolate,
only give up on it. If you run untrusted *Python* callbacks behind your tools,
set it; if your host functions are your own code, the polite tiers are enough.
See `BENCHMARKS.md` for the numbers and
`tests/test_parked_termination.py` for the regression tests.

## Can global-state reset ever be trusted?

**Position: no. Do not build a reset API. It is a trap, and the measurement
below is what a "looks clean but leaks" failure actually looks like.**

Two separate claims, because they have different reasons.

**Within a session, reset is not needed.** It is the same user's own earlier
code. Clearing it is at best a no-op for safety and at worst destroys
legitimate continuity. Skip it.

**Across any boundary, reset cannot be made complete.** The honest reset is
`close()` and a new `Runtime`. To make this concrete rather than
hand-waved, here is the naive reset — delete every own key of `globalThis`,
which is what a first implementation reaches for — applied after a session
polluted the runtime seven different ways:

| Session-1 pollution | Survived the wipe? |
|---|---|
| `Object.prototype.pwned = 'proto-leak'` | **yes** — `({}).pwned` still `'proto-leak'` |
| `Array.prototype.sneak = function(){}` | **yes** — still a `function` |
| `JSON.stringify` hijacked to wrap output | **yes** — still returns `'HIJACKED:{"a":1}'` |
| Pending microtask setting a global later | **yes** — fired after the wipe |
| `globalThis.secret = 'session1-token'` | **yes** — `typeof secret` still `'string'` |
| Closure captured by a bound function | holder unreachable, captured value never freed |
| **The runtime's own usability** | **destroyed** — next eval: `ReferenceError: Array is not defined` |

The last row is the punchline. The naive reset simultaneously **fails to
clear prototype pollution and intrinsic hijacking** *and* **breaks the
runtime**, because deleting own keys of `globalThis` also deletes the
intrinsic bindings (`Array`, `globalThis` itself) that everything including
the bound tool surface depends on. A fresh `Runtime` control run shows
`typeof ({}).pwned === 'undefined'` — genuinely clean.

A *sophisticated* reset could do better than the naive one. It could not do
well enough. To be sound it would have to restore every mutable intrinsic
(`Object.prototype`, `Array.prototype`, `Function.prototype`, `JSON`,
`Promise`, `Reflect`, every getter/setter on every builtin), drain the
isolate-wide microtask queue, discard the module registry, and stay correct
across V8 upgrades that add new intrinsics — i.e. re-implement
`v8::Context::New`, in JS, without V8's help, and keep it correct forever.
V8 already ships that function. Use it: it is called making a new `Context`,
and for a runtime that must also carry ops, the supported way to get one is
a new `Runtime`.

There is no `reset()` on `Runtime` on `main` today (only `close()`,
`python/runtime.rs:169`). That is the correct state. **This design
recommends keeping it that way and documenting why**, so the absence reads
as a decision rather than an omission — exactly the treatment `pool.rs`
already gives "pooled isolates cannot host ops, period, not 'not yet'".

## Snapshots: the other lever

This is the one place the design found more headroom than expected, and it
is independently useful regardless of the affinity work.

**Measured, on `main`'s API:**

- `Runtime(RuntimeConfig(snapshot=…))` + one eval: **1.25 ms**, against
  3.15 ms unsnapshotted. ~60% of cold start recovered, 2.5x.
- **Ops survive snapshotting.** On a snapshot-started runtime,
  `bind_function` works, the bound tool returns the right value, the
  snapshot's own bootstrap globals are present, `register_op(..., mode="async")`
  registers cleanly, and warm tool calls run at 11.0 µs — slightly *faster*
  than on an unsnapshotted runtime.
- Snapshot-started runtimes are also slightly **lighter**: 2.33 MB/session
  versus 2.91 MB at K=64.
- The heavier the bootstrap, the better the trade: a 200 KB bootstrap costs
  7.06 ms from source and 2.27 ms from a snapshot (3.1x).

Why ops survive, stated carefully because the mechanism matters for the
residual risk: `SnapshotBuilder::create_runtime()` (`snapshot.rs:63-83`)
builds `JsRuntimeForSnapshot::try_new(RuntimeOptions { is_main: true,
..Default::default() })` — **with no extensions at all**, while real runtimes
are built with `extensions: vec![python_extension(registry)]`
(`runner.rs:1954`, `runner.rs:2000`). So the snapshot carries only a
bare-bootstrap JS heap, and the two-op shim is installed at runtime startup
on top of it. There is no external-reference mismatch because the snapshot
never contained op bindings to mismatch against. That is *why* it works, and
it also bounds what to promise: snapshotting **with** `python_extension`
registered — to bake the op shim JS into the snapshot too — is a different,
untested operation that would require the runtime's external reference table
to match the builder's exactly, and it is **not** part of this design.

The architecture is unusually friendly to this, and it is worth naming:
peno has exactly **two** static ops (`op_peno_call_python_sync`,
`op_peno_call_python_async`, `ops.rs:132`/`ops.rs:170`), and every
"tool" is a dynamic entry in a `PythonOpRegistry` dispatched through them.
The registry lives in Rust `OpState`, **not in the V8 heap**. So the op
*surface* is fixed and snapshot-compatible while the tool *set* stays fully
dynamic and per-session. Tools never needed to be in the snapshot.

Two constraints to design around:

- `snapshot` and `bootstrap` are mutually exclusive (`config.rs:305`,
  `:396`, `:453`). Per-session dynamic setup therefore has to be an `eval`
  after startup, not a `bootstrap` string. Cheap — that is a 3.77 µs eval.
- `RuntimeConfig` stores `Option<Vec<u8>>` (`config.rs:196`) and
  `snapshot()` clones the whole buffer (`config.rs:440`); `runner.rs:1995`
  then does `SnapshotSource::from_vec`. Building a per-session
  `RuntimeConfig(snapshot=…)` copies 721 KB each time. At K=64 that is
  ~46 MB of duplicated identical bytes. The measured RSS above already
  includes this and is still only 2.33 MB/session, so it is an optimisation
  and not a blocker — but sharing one buffer (`Arc<[u8]>` Rust-side) is the
  obvious follow-up if K ever gets large.

On the prior `will_snapshot` finding: `contributing/upstream-divergence.md` (section 3) established
that `will_snapshot = true` routes isolate creation through V8's
`SnapshotCreator`, which appears to force synchronous compilation and is why
the large-script SIGABRT never reproduced on the builder path. That finding
is about **snapshot build time**, which this design pays once, offline, at
15–39 ms. It does not touch snapshot *consumption*, which is the 1.25 ms
number and the only part on the request path. The two are unrelated costs
and should not be conflated.

## The `deno_core` fork, priced

Vendoring `deno_core` to make `JsRealm` public would enable one `JsRuntime`
to multiplex many `Context`s — true "pooled ops". Priced honestly, and not
declined reflexively; this project has forked a crate before when the
alternative was worse (`contributing/upstream-divergence.md`, section 1).

**What it would buy:** fresh-`Context`-per-call *with* ops available. By
construction that lands at roughly pool cost — the 167 µs checkout is
dominated by `Context` construction plus a channel hop, and adding op
binding to it does not make it cheaper. So the fork's deliverable is a
**167 µs tool call**, to replace a **13.4 µs** one that works today.

That is the whole verdict: **the fork is not merely expensive, it is
pointed the wrong way.** It would make the ops path ~12x slower than the
recommendation in this document, in exchange for cross-session isolation
that per-session affinity already provides for free — and it would provide
it *worse*, since a shared isolate keeps every session's dead contexts on
one heap (the exact accumulation `pool.rs` measures at 130 MB per isolate
over 3000 contexts, and mitigates with `low_memory_notification()`).

The fork's only genuine advantage is **memory density**: one isolate serving
N sessions' contexts costs less than N isolates. At ~2.3 MB/session that
trade starts to matter somewhere north of a thousand concurrent retained
sessions — and a host at that scale has a cheaper answer available first
(smaller K, shorter TTL, accept cold starts), which costs 1.25 ms on a miss
instead of a permanent 12x latency regression on every hit.

**What maintenance would cost, for the record:**

- `deno_core` releases roughly every 2–4 weeks tracking Deno's V8 bumps.
  peno is pinned at `deno_core` 0.409.0 / `v8` 150.4.0. A vendored fork
  means re-applying the `JsRealm` visibility change on every bump the
  project wants to take, forever, or freezing on 0.409 and forgoing V8
  security fixes — a bad look for a sandbox.
- The change is not a one-line `pub`. `JsRealm` is `pub(crate)` because its
  invariants are maintained by surrounding private bookkeeping; exposing it
  means either exporting that bookkeeping too or owning the correctness of
  realm lifecycle by hand. That is the "fast but leaky" shortcut `pool.rs`
  already declined.
- V8 upgrades land precisely in this area (context/realm/snapshot internals
  churn between V8 majors), so the upgrade tax concentrates on the forked
  surface rather than being spread thinly.
- The existing fork this project maintains (`contributing/upstream-divergence.md`, section 1) is a narrow,
  additive termination-handle change. `JsRealm` visibility is a structural
  one. They are not comparable precedents.

**Recommendation: do not fork.** Revisit only if a consuming host
demonstrates >1000 concurrent retained sessions *and* per-session memory is
the binding constraint *and* a 12x tool-call latency regression is
acceptable. Those conditions are unlikely to co-occur.

## What to build

Cheapest useful increment first. Increments 1 and 2 are the "95% of the
value for 5% of the work" the summary promises; 3 is a real but optional
win; 4 and 5 are explicit non-goals recorded so they stop being
re-litigated.

### Increment 1 — Document and benchmark the fast path that already exists (no new code)

The single highest-value change in this document requires no feature work,
because **the fast path already ships**. What is missing is that nobody can
tell: `BENCHMARKS.md` advertises pooling at 16–18x and never benchmarks
"retain the runtime and call the tool again", so a reader reasonably
concludes the ops path is the slow one.

- Add two rows to `BENCHMARKS.md`: warm `Runtime` full tool call (13.4 µs)
  and cold `Runtime` + bind + one tool call (3.14 ms), next to the existing
  pooled checkout row, with one sentence making the comparison explicit —
  *a warm runtime's tool call is ~12x faster than a pooled eval that cannot
  call tools at all.*
- Add the corresponding `benches_py/` case so the number is defended against
  regression, not just asserted once.
- Add a short note to `pool.rs`'s module docs — which already says pooled
  isolates cannot host ops, "period, not 'not yet'" — recording *why that is
  fine*: the ops path's amortised cost is an order of magnitude below the
  pool's, so the missing capability is not a performance gap.

This is hours of work, changes no behaviour, and is what stops the next
contributor from designing a `JsRealm` fork.

### Increment 2 — Session-affine runtime registry

A small, pure-Python keyed cache in `python/peno/`. No Rust.

- `SessionRuntimes(max_sessions=K, idle_ttl=…, factory=…)`: `get(session_id)`
  returns the session's warm `Runtime`, creating it on first use and binding
  that session's tools via the factory.
- LRU eviction at K, plus idle-TTL expiry; eviction calls `close()`.
- A `Runtime` is **never** keyed by anything but session id, and never
  re-keyed. That single invariant is the whole security argument, so it
  should be the thing the tests assert hardest: a runtime handed out for
  session A is never handed out for session B, and an evicted session's next
  call gets a genuinely fresh runtime (assert `typeof ({}).pwned ===
  'undefined'` after a deliberately-polluting session is evicted — the same
  leakage test shape `tests/test_isolate_pool.py` already uses for the pool).
- Document the cold-start-on-miss behaviour as a latency characteristic
  (3.14 ms, or 1.25 ms after increment 3), not an error path.

Deliberately **not** in scope: any reset, any cross-session reuse, any
attempt to make eviction return memory promptly (see deferrals).

### Increment 3 — Snapshot the static bootstrap

Independent of 1 and 2; do it when the first-call latency of a session
actually shows up as a complaint.

- Build one process-wide snapshot at startup covering the static JS the host
  wants in every session (the tool-bridge shim's JS-side helpers,
  polyfills), and start every session runtime from it: 3.15 ms → 1.25 ms.
- Keep per-session dynamic setup as a post-startup `eval`, since `snapshot`
  and `bootstrap` are mutually exclusive.
- Share one snapshot buffer rather than cloning 721 KB per
  `RuntimeConfig` (`config.rs:196`, `:440`).
- Do **not** attempt to snapshot with `python_extension` registered. The
  measured, working configuration is an extension-free snapshot with the op
  shim installed at runtime; baking the shim in is a separate exercise in
  external-reference matching with no measured benefit.

### Increment 4 — Deferred: `reset()`

**Do not build.** Covered above: unnecessary within a session, impossible
across one. The deliverable here is *documentation of the refusal* —
a short note in the architecture docs and the measured pollution-survival
table — so the absence is legible.

### Increment 5 — Deferred: pooled ops / `JsRealm` fork

**Do not build.** Covered above. Revisit only under the three simultaneous
conditions listed in the fork section.

## Deferrals and open questions

Stated explicitly rather than buried, for the record.

- **Does eviction actually return memory?** Untested. The RSS figures are
  high-water marks and cannot answer it. `close()` should drop the isolate
  and join the thread, but the pool's own experience —
  dead contexts needing an explicit `low_memory_notification()` to be
  reclaimed — is a warning that "unreachable" and "freed" differ in V8. If a
  host runs a long-lived process with heavy session churn, measure
  steady-state RSS across many create/evict cycles before trusting K as a
  memory bound. This is the most likely place this design is wrong.
- **K and TTL have no recommended defaults here,** because the right values
  are a property of the host's session-arrival distribution, not of
  peno. The registry should make them explicit constructor arguments
  with no clever defaults.
- **Thread count, not memory, is the scaling ceiling.** Partly measured now.
  The original busy-spin made the *core* count the real ceiling; 0.2.0 removed
  that (0.0% idle CPU and flat per-call latency through K=64, see
  `BENCHMARKS.md`), so K=64 retained runtimes is now demonstrably cheap. Where
  K parked threads *does* start to hurt scheduling is still unmeasured — K=64
  is the largest value tested, and nothing here justifies a K in the thousands.
- **Concurrency within a session is out of scope.** This design assumes
  sequential tool calls within a session, which matches the measured
  `test_steady_state_*` shape. Two concurrent calls on one session's runtime
  serialize on its thread; whether that needs addressing is a host-shaped
  question, not answered here.
- **The absolute microsecond figures are one run on one machine.** The
  ratios are the durable claims. Re-run before making a regression decision
  on different hardware, per `BENCHMARKS.md`'s standing warning.
- **Pre-existing environment flake.** `BENCHMARKS.md` documents an
  intermittent V8-internal `SIGABRT`/tokio-context panic under
  heap-limit and high-concurrency scenarios on `deno_core` 0.409 / `v8`
  150.4.0, unrelated to this work. A session registry holding many runtimes
  raises concurrency, so that flake may surface more often; it should be
  tracked as the known issue it already is rather than mistaken for a bug in
  the registry.

## Mechanisms this design relies on

Every mechanism this design depends on ships in `peno` 0.1.0, so nothing here
is blocked on future work (though 0.2.0 or later is strongly preferable — see
the busy-spin correction above, which is what makes retaining many runtimes
viable at all):

- **Required:** `bind_function`/`register_op`, the two static ops
  (`ops.rs`), `SnapshotBuilder` (`snapshot.rs`), and
  `RuntimeConfig(snapshot=…)` (`config.rs`). Increments 1–3 can be built
  against the current API as-is.
- **Helpful:** `ToolBridge` is the natural thing for the session registry's
  factory to construct per session (call budget, fail-closed name checking),
  and typed tool errors plus console capture make a long-lived session
  runtime far more debuggable than a bare `bind_function` surface. Increment
  2's factory should take a `ToolBridge` rather than raw `bind_function`
  calls.
- **Irrelevant to this design:** the `low_memory_notification()` memory fix
  in `pool.rs`. It applies to pooled isolates recycling contexts, and this
  design does not pool. It is, however, the best available evidence for the
  open question about whether eviction returns memory.
