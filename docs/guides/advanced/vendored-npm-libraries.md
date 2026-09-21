# Running vendored npm libraries safely

`peno` gives sandboxed JS a real V8 engine but no Node.js, no `require()`,
no filesystem, and no network by default. That's exactly the isolation you
want for LLM-generated code -- but a lot of genuinely useful JS lives in npm
packages, not in code the model writes from scratch. This page covers a
pattern for using some of that npm ecosystem *without* weakening the sandbox:
the **host** pre-loads a specific, versioned browser bundle it chooses, and
the guest JS only ever gets to call the API that bundle exposes.

## What makes a library a good candidate

Not every npm package works this way. A library is a good fit when:

- It ships a real, dependency-free **browser or UMD build** -- check its
  package's `dist/` folder, or fetch `https://unpkg.com/<pkg>/dist/...` /
  `https://cdn.jsdelivr.net/npm/<pkg>/dist/...` and look for an IIFE that
  assigns a global (`window.X = ...`), not a bundle that ends in a bare
  `export { ... }` (that's an ES module and needs a build-time rewrite --
  see the `docx` case below).
- Its **core functionality doesn't depend on real `fs`, real network I/O,
  or a DOM**. Document/data-generation libraries -- slide decks, PDFs,
  spreadsheets, schema validation -- tend to qualify, because their job is
  "take data in, produce bytes out." General web-app UI libraries usually
  don't, because they assume a real DOM to render into.
- It can produce its output as an in-memory buffer (base64 string,
  `Uint8Array`, `ArrayBuffer`) rather than only via `fs.writeFile`. Several
  libraries below support both a Node file-writing path and a
  buffer-output method (`.write("base64")`, `.saveAsBase64()`,
  `.writeBuffer()`) -- always use the buffer path.

## The polyfill pattern

A browser bundle typically assumes a handful of browser globals that a bare
V8 isolate doesn't provide. In practice the set needed is small and
library-specific:

```python
from peno import Runtime

polyfills = """
globalThis.setTimeout = function (fn) { fn(); return 0; };
globalThis.clearTimeout = function () {};
// + atob/btoa, or other globals a specific library's bundle checks for
"""

with Runtime() as runtime:
    runtime.eval(polyfills)
    runtime.eval(bundle_source)      # the vetted, versioned bundle text
    result = runtime.eval_async(js_that_calls_the_library_api)
```

The host decides what `bundle_source` is (a file it ships, or a pinned URL it
fetches once) -- the guest JS never chooses what gets loaded.

## Verified examples

`examples/vendored_npm_libraries.py` runs this pattern end-to-end for two
libraries and checks the output is structurally real (a valid OOXML zip / a
valid PDF), not just "didn't throw":

| Library | Real browser/UMD build? | Polyfills needed | Result |
|---|---|---|---|
| [`pptxgenjs`](https://www.npmjs.com/package/pptxgenjs) | Yes (`dist/pptxgen.bundle.js`) | `setTimeout`/`clearTimeout`, `atob`/`btoa` | Builds a real multi-slide `.pptx` (title, table, native chart, image) |
| [`pdf-lib`](https://www.npmjs.com/package/pdf-lib) | Yes (`dist/pdf-lib.min.js`) | None | Builds a real multi-page `.pdf` |
| [`exceljs`](https://www.npmjs.com/package/exceljs) | Yes (`dist/exceljs.min.js`) | `setTimeout`/`clearTimeout`, `btoa` (for base64 output) | Builds a real `.xlsx` via `workbook.xlsx.writeBuffer()` (avoid its `fs`-based write paths) |
| [`zod`](https://www.npmjs.com/package/zod) | Yes (`lib/index.umd.js`) | None | Runs real schema validation, no I/O at all |
| [`docx`](https://www.npmjs.com/package/docx) (dolanmiu/docx) | **No** -- `build/index.js` is an ES module ending in `export { ... }`, not an IIFE | Same as above, plus a source rewrite of the trailing `export { A, B as C }` into `globalThis.docx = { A, C: B }` (valid because both are identifier-list grammars) | Document construction works after the rewrite, but `Packer.toBase64String()` never resolved its promise in testing -- likely something in its zip/compression pipeline expects a browser or Node capability this pattern doesn't provide. **Not currently recommended** without further investigation into that hang. |

### pptxgenjs and Anthropic's published `pptx` skill

Anthropic's [`pptx` skill](https://github.com/anthropics/skills/tree/main/skills/pptx)
has two halves, and only one of them is relevant to this pattern:

- **Deck creation** is a `pptxgenjs` script (the skill's `SKILL.md` documents
  the real API usage: `pres.layout`, `addSlide()`, `addText()`, `addTable()`,
  `addChart()`, `addImage()`, hex colors, speaker notes via `addNotes()`).
  This half runs unmodified inside a `peno.Runtime` with the two
  polyfills above -- verified here with a multi-slide deck containing a
  title slide, a table, a native chart, and an image, round-tripped through
  `python-pptx` to confirm it's a real, openable file.
- **Everything else in the skill is Python, not JS**, and has nothing to do
  with a JS sandbox: `scripts/thumbnail.py` (LibreOffice + Pillow rendering),
  `scripts/office/validate.py` (schema/relationship validation),
  `scripts/office/soffice.py` (LibreOffice conversion), `scripts/add_slide.py`
  and `scripts/clean.py` (raw OOXML XML manipulation). None of that runs in
  peno, and it isn't meant to -- don't confuse "the JS half of one
  skill works in a JS sandbox" with "the whole skill runs in peno."

## What this is NOT

This is **not** the same as giving guest JS a real `require()` or npm
resolver. There is no dynamic module resolution happening on the guest's
behalf, and guest code can never load anything the host didn't explicitly
provide -- the host chooses the exact bundle text (a specific version, often
pinned by URL or vendored on disk) before any guest code runs. A malicious
or buggy script running inside the sandbox cannot reach out and pull in an
arbitrary package; it can only call the API surface of whatever the host
already `eval`'d.

This is also **not yet a polished, automatic feature**. There is no
`Runtime(allow_modules=["pptxgenjs"])`-style API today -- the host has to
write its own polyfill script and its own `eval()`/`eval_async()` calls, as
shown above. Building a first-class "allow-listed vendored module" API (one
that ships known-good bundles + polyfill sets for a curated library list)
is real future work, not something this pattern already automates.
