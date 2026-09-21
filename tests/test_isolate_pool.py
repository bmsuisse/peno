"""Tests for `peno.IsolatePool` -- the pooled, isolate-reuse-with-
fresh-context fast path (see `src/runtime/pool.rs` for the design).

The security property test (`test_no_global_state_leaks_across_reused_isolate`)
is the most important test in this file: it proves that reusing a warm V8
isolate across two logically separate `eval` calls never leaks JS-visible
global state between them, which is the whole safety argument for this
feature existing at all.
"""

from __future__ import annotations

import pytest

from peno import IsolatePool


def test_pool_eval_basic() -> None:
    pool = IsolatePool(size=2)
    isolate = pool.checkout()
    assert isolate.eval("1 + 41") == 42
    assert isolate.eval("'a' + 'b'") == "ab"
    isolate.release()


def test_pool_prewarms_and_tracks_idle_count() -> None:
    pool = IsolatePool(size=3)
    assert pool.idle_count() == 3

    isolate = pool.checkout()
    assert pool.idle_count() == 2

    isolate.release()
    assert pool.idle_count() == 3


def test_pool_context_manager_releases_automatically() -> None:
    pool = IsolatePool(size=1)
    with pool.checkout() as isolate:
        assert pool.idle_count() == 0
        assert isolate.eval("6 * 7") == 42
    assert pool.idle_count() == 1


def test_pool_release_without_context_manager_via_gc() -> None:
    pool = IsolatePool(size=1)
    isolate = pool.checkout()
    assert pool.idle_count() == 0
    isolate.eval("1")
    del isolate  # dropping without explicit release() still returns it
    assert pool.idle_count() == 1


def test_pool_never_blocks_when_exhausted() -> None:
    """Checking out beyond pool capacity must spin up a fresh isolate, not block."""
    pool = IsolatePool(size=1)
    held = pool.checkout()
    assert pool.idle_count() == 0

    # Must return immediately with a working isolate, not deadlock.
    overflow = pool.checkout()
    assert overflow.eval("2 + 2") == 4

    held.release()
    overflow.release()


def test_pool_surfaces_javascript_errors() -> None:
    pool = IsolatePool(size=1)
    with pool.checkout() as isolate:
        with pytest.raises(Exception):
            isolate.eval("throw new Error('boom')")


def test_no_global_state_leaks_across_reused_isolate() -> None:
    """The core security property: checking out the *same* isolate again
    (guaranteed here by using a pool of size 1) must not see globals set by
    a previous, logically separate checkout.
    """
    pool = IsolatePool(size=1)

    first = pool.checkout()
    assert first.eval("globalThis.leaked = 'secret'; globalThis.leaked") == "secret"
    first.release()

    second = pool.checkout()
    assert second.eval("typeof leaked") == "undefined"
    second.release()


def test_no_state_persists_between_evals_on_the_same_checkout() -> None:
    """Even within a single checkout, `eval()` is stateless across calls --
    each call gets its own fresh context (unlike `Runtime.eval`, which keeps
    one persistent context for the runtime's whole life)."""
    pool = IsolatePool(size=1)
    with pool.checkout() as isolate:
        isolate.eval("var counter = 1;")
        assert isolate.eval("typeof counter") == "undefined"
