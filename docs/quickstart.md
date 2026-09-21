# Quick Start

## Installation

Create or activate a virtual environment inside your project and install the package:

```bash
python -m venv .venv
source .venv/bin/activate

pip install peno  # or `uv install peno`
```

!!! warning "Platform Support"

    Supports macOS (Apple Silicon) and Linux (x86_64, ARM64) with glibc ([manylinux](https://github.com/pypa/manylinux)). Windows and musl-based distributions (e.g., Alpine) are not supported currently.


## Run JavaScript from Python

Use [`peno.eval`][peno.eval] to evaluate JavaScript code directly:

```python
>>> import peno
>>>
>>> print(peno.eval("2 + 2"))
4
>>> print(peno.eval("Math.sqrt(25)"))
5
```

`peno.eval()` runs synchronously and returns the result immediately.

## Share functions and data

Bind Python callables or objects so they are visible from JavaScript:

```python
import peno

peno.bind_function("notify", lambda msg: print("JS:", msg))
peno.eval("notify('hello from JS')")

peno.bind_object("config", {"debug": True})
peno.eval("config.debug")
```

Once bound, they will remain available to all subsequent evaluations.

## Run code asynchronously

Use `eval_async` to run JavaScript without blocking Python’s event loop.
This is useful when JavaScript performs long-running work or returns a [Promise][promise].

```python
import asyncio

async def main():
    # JavaScript expression
    result = await peno.eval_async("42")

    # Code that returns a Promise is also supported
    result = await peno.eval_async("Promise.resolve('done')")
    print(result)

asyncio.run(main())
```

## Keep types familiar

`peno` automatically converts common data types between Python and JavaScript, so you can work with familiar types on both sides. Numbers, strings, booleans, lists, dictionaries, and more are converted seamlessly.

```python
# Python → JavaScript → Python
result = peno.eval("[1, 2, 3].map(x => x * 2)")
print(result)  # [2, 4, 6]
```

For complete details on type conversion, including special types like `undefined`, `BigInt`, `Date`, and binary data, see [Type Conversion](concepts/types.md).

## Working with Runtime directly

!!! note "peno vs. Runtime"

    The [`peno`][peno] module provides a convenient interface that automatically manages a context-local [`Runtime`][peno.Runtime] for you.
    Each asyncio task or thread gets its own isolated runtime instance, created lazily and cleaned up automatically.
    This makes it perfect for everyday use where you just want to run JavaScript without managing runtime lifecycle.

For more control, you can work with the [`Runtime`][peno.Runtime] class directly:

```python
from peno import Runtime

runtime = Runtime()  # Create a runtime instance

runtime.eval("let counter = 0")
runtime.eval("counter++")
print(runtime.eval("counter"))  # 1

runtime.close()  # Clean up when done
```

Or use it as a context manager for automatic cleanup:

```python
with Runtime() as runtime:
    print(runtime.eval("42"))
```

Each [`Runtime`][peno.Runtime] runs on a dedicated thread with its own V8 isolate, where state persists across evaluations.

## Next steps

- Learn more about [`Runtime`][peno.Runtime] in [Concepts](concepts/runtime.md)

[promise]: https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Promise
