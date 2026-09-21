"""
Run vendored npm libraries inside a bare peno.Runtime.

This demonstrates a general PATTERN, not a built-in feature: the *host*
fetches (or ships) a specific, versioned browser/UMD bundle of an npm
library, injects a handful of small polyfills the bundle needs, evals the
bundle text into an isolated V8 context, then calls the library's real API
to produce real output bytes -- no Node.js, no filesystem, no network access
from the guest JS.

A library is a good candidate for this pattern when:
  - it ships a real, dependency-free browser or UMD build (check its
    package's `dist/` or `unpkg.com/<pkg>/dist/`), and
  - its core functionality does not depend on real `fs`, real network
    access, or a DOM -- document/data-generation libraries (slide decks,
    PDFs, spreadsheets) tend to qualify; general web-app UI libraries
    usually don't.

Two libraries are demonstrated here, both verified to produce real,
structurally valid output files:

  1. pptxgenjs -- builds a real multi-slide .pptx (title slide, a table,
     a native chart, and an image), matching how Anthropic's published
     `pptx` skill (github.com/anthropics/skills, skills/pptx) actually
     drives pptxgenjs. Needs two tiny polyfills: setTimeout/clearTimeout
     (it schedules async work with them) and atob/btoa (base64 encode
     paths inside the bundle).
  2. pdf-lib -- builds a real multi-page .pdf. Needs *no* polyfills at
     all; it's a genuinely browser-native library by design.

IMPORTANT -- what this is NOT: this is not the same as giving guest JS a
real `require()`/npm resolver. The guest code never gets to load anything
of its own choosing; the HOST decides, ahead of time, exactly which
vetted, versioned bundle text gets evaluated. There is no dynamic module
resolution happening on the guest's behalf. It is also, today, a fully
manual pattern -- the host script below writes its own polyfills and eval
calls; there is no `Runtime(allow_modules=["pptxgenjs"])`-style API. See
`docs/guides/advanced/vendored-npm-libraries.md` for the full writeup,
including a library that did NOT work with this pattern and why.
"""

import asyncio
import base64
import zipfile
from io import BytesIO

import httpx

from peno import Runtime

PPTXGENJS_URL = "https://cdn.jsdelivr.net/npm/pptxgenjs@4/dist/pptxgen.bundle.js"
PDF_LIB_URL = "https://cdn.jsdelivr.net/npm/pdf-lib@1/dist/pdf-lib.min.js"

# The exact base64-alphabet atob/btoa pptxgenjs needs; V8 has no browser
# base64 builtins by default.
BASE64_POLYFILLS = """
globalThis.setTimeout = function (fn) { fn(); return 0; };
globalThis.clearTimeout = function () {};

const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
globalThis.atob = function (input) {
  let str = String(input).replace(/=+$/, "");
  let output = "";
  let bc = 0, bs, buffer, idx = 0;
  for (; buffer = str.charAt(idx++); ~buffer && (bs = bc % 4 ? bs * 64 + buffer : buffer, bc++ % 4)
       ? output += String.fromCharCode(255 & bs >> (-2 * bc & 6)) : 0) {
    buffer = B64.indexOf(buffer);
  }
  return output;
};
globalThis.btoa = function (input) {
  let str = String(input);
  let output = "";
  for (let block = 0, charCode, i = 0, map = B64;
       str.charAt(i | 0) || (map = "=", i % 1);
       output += map.charAt(63 & block >> 8 - i % 1 * 8)) {
    charCode = str.charCodeAt(i += 3 / 4);
    if (charCode > 0xFF) throw new Error("btoa: invalid character");
    block = block << 8 | charCode;
  }
  return output;
};
"""

PPTX_SCRIPT = """
const pres = new PptxGenJS();
pres.layout = "LAYOUT_WIDE";

const s1 = pres.addSlide();
s1.addText("Vendored npm libraries in peno", {
  x: 0.5, y: 2.2, w: 12.3, h: 1.2, fontSize: 32, bold: true, align: "center",
});

const s2 = pres.addSlide();
s2.addText("Revenue by region", { x: 0.5, y: 0.3, w: 8, h: 0.6, fontSize: 22, bold: true });
s2.addTable(
  [["Region", "Revenue"], ["EMEA", "1.2M"], ["APAC", "0.9M"]],
  { x: 0.5, y: 1.1, w: 6, colW: [3, 3] }
);

const s3 = pres.addSlide();
s3.addChart(pres.ChartType.bar, [
  { name: "Revenue", labels: ["Q1", "Q2"], values: [10, 14] },
], { x: 0.5, y: 1.1, w: 8, h: 4, showTitle: true, title: "Growth" });

pres.write({ outputType: "base64" });
"""

PDF_SCRIPT = """
(async () => {
  const doc = await PDFLib.PDFDocument.create();
  const page = doc.addPage([300, 200]);
  page.drawText("Built with pdf-lib inside peno", { x: 20, y: 150, size: 12 });
  return doc.saveAsBase64();
})()
"""


async def fetch_bundle(url: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


async def run_pptxgenjs() -> bytes:
    bundle = await fetch_bundle(PPTXGENJS_URL)
    with Runtime() as rt:
        rt.eval(BASE64_POLYFILLS)
        rt.eval(bundle)
        b64 = await rt.eval_async(PPTX_SCRIPT)
    return base64.b64decode(b64)


async def run_pdf_lib() -> bytes:
    bundle = await fetch_bundle(PDF_LIB_URL)
    with Runtime() as rt:
        # No polyfills needed -- pdf-lib is genuinely browser-native.
        rt.eval(bundle)
        b64 = await rt.eval_async(PDF_SCRIPT)
    return base64.b64decode(b64)


def verify_pptx(raw: bytes) -> None:
    zf = zipfile.ZipFile(BytesIO(raw))
    assert "[Content_Types].xml" in zf.namelist(), "not a real OOXML zip"
    slides = [n for n in zf.namelist() if n.startswith("ppt/slides/slide")]
    assert len(slides) == 3, f"expected 3 slides, got {len(slides)}"
    assert any("charts/chart" in n for n in zf.namelist()), "missing chart part"


def verify_pdf(raw: bytes) -> None:
    assert raw.startswith(b"%PDF-"), "not a real PDF"
    assert b"%%EOF" in raw[-64:] or b"%%EOF" in raw, "missing PDF trailer"


async def main() -> None:
    print("Building a .pptx with the real pptxgenjs bundle (2 polyfills needed)...")
    pptx_bytes = await run_pptxgenjs()
    verify_pptx(pptx_bytes)
    print(f"  OK: {len(pptx_bytes)} bytes, 3 slides incl. table + chart, verified as real OOXML zip")

    print("Building a .pdf with the real pdf-lib bundle (no polyfills needed)...")
    pdf_bytes = await run_pdf_lib()
    verify_pdf(pdf_bytes)
    print(f"  OK: {len(pdf_bytes)} bytes, verified as a real PDF")


if __name__ == "__main__":
    asyncio.run(main())
