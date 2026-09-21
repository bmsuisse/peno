"""Basic usage of peno - showcasing both context-local API and explicit Runtime."""

import peno
from peno import Runtime


def context_local_example():
    """Using the context-local API (recommended for simple cases)."""
    print("=== Context-Local API ===")
    print("The easiest way - automatic per-task/thread isolation\n")

    # Simple evaluation
    print("2 + 2 =", peno.eval("2 + 2"))
    print("Math.sqrt(25) =", peno.eval("Math.sqrt(25)"))

    # State persists across evaluations in the same context
    peno.eval("globalThis.counter = 0;")
    for _ in range(3):
        print("counter ->", peno.eval("++counter"))

    print()


def explicit_runtime_example():
    """Using explicit Runtime for more control."""
    print("=== Explicit Runtime API ===")
    print("More control over runtime lifecycle\n")

    with Runtime() as rt:
        print("2 + 2 =", rt.eval("2 + 2"))
        rt.eval("globalThis.counter = 0;")
        for _ in range(3):
            print("counter ->", rt.eval("++counter"))

    print()


def main() -> None:
    """
    peno provides two ways to run JavaScript:

    1. Context-local API (peno.eval): Automatic runtime management per task/thread
       - Perfect for simple scripts and interactive sessions
       - Each asyncio task or thread gets its own isolated runtime
       - Cleanup happens automatically

    2. Explicit Runtime: Manual control over runtime lifecycle
       - Use when you need to share state across multiple operations
       - More control over configuration (memory limits, timeouts, etc.)
       - Must manage cleanup (use context manager or call .close())
    """
    context_local_example()
    explicit_runtime_example()


if __name__ == "__main__":
    main()
