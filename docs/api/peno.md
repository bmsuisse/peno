# `peno`

The top-level package provides convenience functions that automatically manage a context-local [`Runtime`][peno.Runtime] instance for you.
Each asyncio task or thread gets its own isolated runtime, created lazily and cleaned up automatically.

::: peno
    options:
      members:
        - eval
        - eval_async
        - bind_function
        - bind_object
        - get_default_runtime
        - close_default_runtime
