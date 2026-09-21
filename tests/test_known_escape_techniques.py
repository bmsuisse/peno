"""Regression tests for *named, publicly disclosed* sandbox escape techniques.

This file is a growing catalogue, not a one-time audit. It replaces the ad hoc
`test_security_audit.py`.

## Convention for contributors (please follow it)

When a new JS/sandbox escape is disclosed and triaged, add a **new class
here** rather than a new loose test file, so the catalogue stays in one
place and every entry carries the same metadata. One class per named
technique, with this docstring template:

    class TestTechniqueName:
        \"\"\"One-line summary of the technique.

        Source:      <URL or CVE>
        Disclosed:   <YYYY-MM>
        Root cause:  <the *class* of mistake, not the specific payload>
        Relevance:   <why peno's design does or does not avoid it>
        Status:      not-applicable | mitigated | mitigated-by-design | VULNERABLE
        \"\"\"

Guidance that keeps this file useful rather than decorative:

- Test the **root cause class**, not the literal payload. A payload stops
  working for uninteresting reasons; the design property it attacked is what
  must keep holding.
- If a technique genuinely cannot apply because the surface does not exist
  (no filesystem, no network), say so in `Status: not-applicable` and still
  add an assertion pinning the *absence* of that surface -- that is what
  turns "we don't have that feature" into a test that fails if someone adds
  it without a permission model.
- Prefer testing through the public Python API, the way a user is exposed.
"""

from __future__ import annotations

import posixpath

import pytest

from peno import IsolatePool, JavaScriptError, Runtime, RuntimeConfig


def _fresh_runtime(**kwargs: object) -> Runtime:
    return Runtime(RuntimeConfig(**kwargs))  # type: ignore[arg-type]


class TestHeapjack:
    """Secret exfiltration from a V8 heap shared between trust domains.

    Source:      https://www.bleepingcomputer.com/news/security/researchers-escape-openai-codex-sandbox-to-run-commands-on-host/
    Disclosed:   2025
    Root cause:  Two trust domains (trusted orchestrator, untrusted guest
                 code) sharing ONE V8 Isolate/heap, plus guest-reachable heap
                 introspection (`v8.getHeapSnapshot()`). A secret auth token
                 lived in the shared heap; untrusted JS snapshotted the heap,
                 brute-forced the token out of it, and used it to impersonate
                 the trusted side over IPC.
    Relevance:   peno never puts a secret in the JS heap. Authorization
                 is "which op_id the host chose to bind", resolved in a
                 Rust-side registry (`PythonOpRegistry`, src/runtime/ops.rs)
                 that guest JS cannot read or forge. And each `Runtime` gets
                 its own isolate on its own OS thread, so there is no shared
                 heap to snapshot in the first place.
    Status:      mitigated-by-design
    """

    def test_no_v8_or_node_heap_introspection_globals(self) -> None:
        """Heapjack's first step must not be reachable at all."""
        with _fresh_runtime() as rt:
            result = rt.eval(
                "[typeof v8, typeof process, typeof require, typeof Deno].join(',')"
            )
        assert result == "undefined,undefined,undefined,undefined", (
            f"guest JS can see a debug/introspection global: {result!r}"
        )

    def test_v8_natives_syntax_is_not_enabled(self) -> None:
        """`%DebugPrint` and friends require --allow-natives-syntax; without
        it the reference is a SyntaxError, which is the desired outcome."""
        with _fresh_runtime() as rt:
            with pytest.raises(JavaScriptError):
                rt.eval("%DebugPrint(1)")

    def test_no_heap_snapshot_capability_via_any_bound_object(self) -> None:
        """`bind_object`/`bind_function` install only value/op entries, never
        a raw V8 internals handle."""
        with _fresh_runtime() as rt:
            rt.bind_function("add", lambda a, b: a + b)
            assert rt.eval(
                "typeof require === 'undefined' && typeof process === 'undefined'"
            )

    def test_host_op_authorization_is_not_js_observable(self) -> None:
        """The op_id -> Python-handler mapping must not leak into guest JS."""
        with _fresh_runtime() as rt:
            rt.bind_function("secretOp", lambda: "should-not-leak")
            rt.bind_function("publicOp", lambda x: x * 2)

            source = rt.eval("secretOp.toString()")
            assert "op_id" not in source

            # `Deno`/`Deno.core.ops` is deleted by the bridge bootstrap before
            # any guest code runs, so there is no ops table to forge a call
            # against -- the only surface is the closures the host installed.
            assert rt.eval("typeof Deno") == "undefined"
            assert rt.eval("publicOp(21)") == 42

    def test_isolate_state_does_not_leak_between_runtimes(self) -> None:
        """The structural check for Heapjack's root cause: separate trust
        domains must not share a heap."""
        rt_a = _fresh_runtime()
        rt_b = _fresh_runtime()
        try:
            rt_a.bind_function("leakMe", lambda: "runtime-a-secret")
            assert rt_b.eval("typeof leakMe") == "undefined"

            rt_a.eval("globalThis.__marker = 'a-only'")
            assert rt_b.eval("typeof globalThis.__marker") == "undefined"
        finally:
            rt_a.close()
            rt_b.close()

    def test_pooled_isolate_reuse_does_not_share_globals(self) -> None:
        """`IsolatePool` deliberately *does* reuse one isolate across logically
        separate callers, which is the exact shape Heapjack exploited. It is
        safe only because every checkout gets a brand-new `v8::Context`, so
        this property is the one that must never regress.
        """
        pool = IsolatePool(size=1)  # size 1: the same isolate comes back.

        first = pool.checkout()
        assert first.eval("globalThis.token = 'sk-secret'; globalThis.token") == (
            "sk-secret"
        )
        first.release()

        second = pool.checkout()
        assert second.eval("typeof token") == "undefined"
        assert second.eval("typeof globalThis.token") == "undefined"
        second.release()

    def test_host_callback_secrets_stay_in_python(self) -> None:
        """A closure's captured secret must not be reachable from JS.

        This is the positive form of Heapjack's lesson: keep the secret on the
        host side of the FFI boundary, where guest JS has no representation
        for it at all.
        """
        secret = "sk-do-not-leak"

        def authorized_action(argument: str) -> str:
            return f"ok:{argument}" if secret else "denied"

        with _fresh_runtime() as rt:
            rt.bind_function("action", authorized_action)
            assert rt.eval("action('x')") == "ok:x"
            # Nothing in JS's view of the function exposes the captured value.
            assert secret not in rt.eval("action.toString()")
            assert rt.eval("Object.keys(action).length") == 0


class TestOverpatch:
    """Over-broad permission scoping derived from user-supplied paths.

    Source:      https://www.bleepingcomputer.com/news/security/researchers-escape-openai-codex-sandbox-to-run-commands-on-host/
    Disclosed:   2025
    Root cause:  A filesystem tool granted write access to "the parent folder
                 of any path named in a request", so naming `/tmp` granted
                 write access to the whole filesystem root's children. The
                 class of mistake is deriving a permission *scope* from
                 attacker-influenced input.
    Relevance:   peno ships no filesystem or network surface at all, by
                 design, so there is no permission scope to widen. The tests
                 below pin that absence: they fail if someone adds a
                 filesystem/network capability without a permission model,
                 which is the moment this technique becomes applicable.
    Status:      not-applicable (no such capability exists)
    """

    @pytest.mark.parametrize(
        "probe",
        [
            "typeof require",
            "typeof process",
            "typeof Deno",
            "typeof fetch",
            "typeof XMLHttpRequest",
            "typeof WebSocket",
            "typeof importScripts",
            "typeof Worker",
        ],
    )
    def test_no_filesystem_or_network_surface_exists(self, probe: str) -> None:
        """If any of these becomes defined, a permission model is required
        before shipping it -- and this test is the tripwire."""
        with _fresh_runtime() as rt:
            assert rt.eval(probe) == "undefined", (
                f"{probe} is now defined; a capability/permission model is "
                "needed before exposing it, or Overpatch becomes applicable"
            )

    def test_host_tools_receive_arguments_not_authority(self) -> None:
        """A bound tool's authority is fixed at bind time by the host.

        Guest JS chooses the *arguments*, never the scope: it cannot widen
        what the tool is allowed to touch, which is precisely the property
        Overpatch's design lacked. Enforcement therefore has to live in the
        Python tool, and this test demonstrates it doing so.
        """
        allowed_prefix = "/data/"
        reads: list[str] = []

        def read_scoped(path: str) -> str:
            # Normalize *before* checking. A bare `startswith` is itself the
            # Overpatch bug: "/data/../etc/passwd" passes a prefix test while
            # resolving outside the intended scope.
            resolved = posixpath.normpath(path)
            if not resolved.startswith(allowed_prefix):
                raise PermissionError(f"path outside {allowed_prefix}: {path}")
            reads.append(resolved)
            return "contents"

        with _fresh_runtime() as rt:
            rt.bind_function("readFile", read_scoped)
            assert rt.eval("readFile('/data/ok.txt')") == "contents"

            # The Overpatch payload shape: escape the scope via a parent path.
            outcome = rt.eval("""
              try { readFile('/data/../etc/passwd'); 'ESCAPED' }
              catch (e) { e.name }
            """)

        assert outcome == "PermissionError", (
            f"a scope-escaping path was not refused: {outcome!r}"
        )
        assert reads == ["/data/ok.txt"]

    def test_guest_js_cannot_rebind_a_host_tool_to_widen_it(self) -> None:
        """Reassigning the global must not affect what the host registered."""

        def scoped(path: str) -> str:
            if not path.startswith("/data/"):
                raise PermissionError("denied")
            return "contents"

        with _fresh_runtime() as rt:
            rt.bind_function("readFile", scoped)
            # Guest overwrites the global in its own context...
            rt.eval("globalThis.readFile = () => 'forged'")
            # ...which cannot change the Rust-side registry entry, so a fresh
            # binding of the same op still enforces the host's scope.
            rt.bind_function("readFile2", scoped)
            assert (
                rt.eval(
                    "try { readFile2('/etc/passwd'); 'ESCAPED' } catch (e) { e.name }"
                )
                == "PermissionError"
            )


class TestErrorMessageDisclosure:
    """Host-path/internals disclosure through exception text.

    Source:      generic hardening class (no single disclosure); opportunistic
                 finding from the 2026-09 audit that produced this file
    Disclosed:   n/a
    Root cause:  Errors crossing a sandbox boundary carrying host filesystem
                 paths or internal source locations, which hands an attacker
                 a map of the host for a follow-up exploit.
    Relevance:   JS exceptions surfaced to Python should carry JS-side stack
                 info (script name, line) and not Rust source paths or
                 absolute host paths.
    Status:      mitigated
    """

    def test_js_errors_do_not_leak_host_paths_or_rust_internals(self) -> None:
        with _fresh_runtime() as rt:
            with pytest.raises(JavaScriptError) as caught:
                rt.eval("throw new Error('boom')")

        text = str(caught.value)
        assert "src/runtime" not in text
        assert "/Users/" not in text
        assert "/home/" not in text
        assert ".cargo" not in text

    def test_host_tool_exceptions_do_not_leak_python_tracebacks(self) -> None:
        """A tool's exception reaches JS as name + message only.

        The typed-error path (v0.3) deliberately forwards the exception's
        class name and message, and nothing else -- no traceback, no module
        paths, no local variables.
        """

        def failing() -> None:
            raise ValueError("just the message")

        with _fresh_runtime() as rt:
            rt.bind_function("failing", failing)
            details = rt.eval("""
              try { failing(); 'no-throw' }
              catch (e) { JSON.stringify({name: e.name, message: e.message,
                                          stack: String(e.stack || '')}) }
            """)

        assert '"name":"ValueError"' in details
        assert '"message":"just the message"' in details
        # Note: `ext:peno/python_bridge.js` may appear in a stack. That is
        # the extension's virtual module specifier, not a host filesystem
        # path, so it is not a disclosure -- only real host paths and Python
        # traceback machinery are.
        for leak in ("Traceback", "site-packages", "/Users/", "/home/", ".cargo"):
            assert leak not in details, f"tool error leaked {leak!r}: {details}"


class TestResourceExhaustion:
    """Denial of service via unbounded guest resource use.

    Source:      generic hardening class
    Disclosed:   n/a
    Root cause:  A sandbox that cannot be *stopped* is not a sandbox: guest
                 code that loops forever or allocates without bound takes the
                 host down with it.
    Relevance:   peno has a cross-thread termination handle (PATCH.md)
                 and a heap limit that terminates execution. These are the
                 regression tests for that being reachable from the public
                 API; the fuzz suite generates many more such programs.
    Status:      mitigated
    """

    def test_an_infinite_loop_is_stopped_by_the_timeout(self) -> None:
        with Runtime(RuntimeConfig(timeout=1.0)) as rt:
            with pytest.raises((JavaScriptError, RuntimeError)):
                rt.eval("while (true) {}")

    def test_unbounded_allocation_is_stopped_by_the_heap_limit(self) -> None:
        config = RuntimeConfig(max_heap_size=16 * 1024 * 1024, timeout=10.0)
        with Runtime(config) as rt:
            with pytest.raises((JavaScriptError, RuntimeError, MemoryError)):
                rt.eval("const a = []; while (true) a.push('x'.repeat(10000));")

    def test_the_process_survives_a_terminated_runtime(self) -> None:
        """Termination must kill the eval, not the host."""
        with Runtime(RuntimeConfig(timeout=1.0)) as rt:
            with pytest.raises((JavaScriptError, RuntimeError)):
                rt.eval("while (true) {}")
        # A brand-new runtime still works, so the process and V8 platform are
        # intact after a forced termination.
        with Runtime() as fresh:
            assert fresh.eval("1 + 1") == 2
