//! `IsolatePool` bindings: pooled, isolate-reuse-with-fresh-context eval.
//!
//! See `src/runtime/pool.rs` for the reuse boundary this implements and why
//! it's safe. This is deliberately a narrower surface than `Runtime`: plain
//! synchronous `eval()` only, JSON-safe values only, no ops/modules/bindings.

use crate::runtime::conversion::js_value_to_python;
use crate::runtime::pool::{IsolatePool, PooledIsolate};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use std::sync::Mutex;

use super::error::runtime_error_with_context;

/// A small pool of pre-warmed V8 isolates for fast, context-isolated `eval`.
///
/// Every [`checkout`](Self::checkout) starts in a brand-new, empty JS
/// `Context` -- no global state is visible across two checkouts, even when
/// they reuse the same underlying isolate. See `src/runtime/pool.rs` for the
/// full reasoning.
#[pyclass(name = "IsolatePool", module = "peno")]
pub struct IsolatePoolPy {
    inner: IsolatePool,
}

#[pymethods]
impl IsolatePoolPy {
    #[new]
    #[pyo3(signature = (size = 4))]
    fn new(size: usize) -> Self {
        Self {
            inner: IsolatePool::new(size),
        }
    }

    /// Number of pre-warmed isolates currently idle in the pool.
    fn idle_count(&self) -> usize {
        self.inner.idle_count()
    }

    /// Check out a warm isolate (or spin up a fresh one if the pool is
    /// exhausted -- this never blocks).
    fn checkout(&self) -> PooledIsolatePy {
        PooledIsolatePy {
            inner: Mutex::new(Some(self.inner.checkout())),
        }
    }

    /// Shut down every currently-idle pooled isolate.
    fn close(&self) {
        self.inner.close();
    }
}

/// One checked-out isolate from an [`IsolatePoolPy`].
///
/// Use as a context manager, or call [`release`](Self::release) explicitly;
/// dropping it without releasing returns it to the pool automatically.
#[pyclass(name = "PooledIsolate", module = "peno")]
pub struct PooledIsolatePy {
    inner: Mutex<Option<PooledIsolate>>,
}

#[pymethods]
impl PooledIsolatePy {
    /// Evaluate `code` in a fresh `Context` on this isolate. Only JSON-safe
    /// return values are supported (numbers, strings, booleans, null,
    /// arrays, plain objects) -- functions, streams, and other host-bound
    /// values from the full `Runtime` API are not.
    fn eval(&self, py: Python<'_>, code: &str) -> PyResult<Py<PyAny>> {
        let code_owned = code.to_owned();
        let js_value = py.detach(|| {
            let guard = self.inner.lock().unwrap();
            let pooled = guard.as_ref().ok_or_else(|| {
                PyRuntimeError::new_err("PooledIsolate has already been released")
            })?;
            pooled
                .eval(&code_owned)
                .map_err(|e| runtime_error_with_context("Pooled evaluation failed", e))
        })?;
        js_value_to_python(py, &js_value, None)
    }

    /// Return this isolate to the pool for reuse. Idempotent; also happens
    /// automatically on drop/garbage collection if not called explicitly.
    fn release(&self) {
        self.inner.lock().unwrap().take();
    }

    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    #[pyo3(signature = (_exc_type=None, _exc_value=None, _traceback=None))]
    fn __exit__(
        &self,
        _exc_type: Option<&Bound<'_, PyAny>>,
        _exc_value: Option<&Bound<'_, PyAny>>,
        _traceback: Option<&Bound<'_, PyAny>>,
    ) -> bool {
        self.release();
        false
    }
}
