"""/ocr -- make a PDF searchable (invisible text layer) via OCRmyPDF + Tesseract.

Free, no API keys. Existing text is NEVER touched:
  --redo-ocr    keeps all existing real text; additionally OCRs text that lives in
                images/scans on ANY page (mixed pages included)
  --optimize 0  no image recompression
  --output-type pdf  no PDF/A conversion (keeps the file as close to original)
Only the requested pages (--pages) are OCR'd; other pages pass through untouched.
"""
import asyncio
import os
import re
import tempfile

_SPEC_RE = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")


def parse_ocr_args(text: str):
    """'/ocr 1-5,8' | '/ocr -p 3' | '/ocr' -> (spec or None, error or None)."""
    arg = re.sub(r"^/ocr(@\w+)?", "", (text or "").strip(), flags=re.I).strip()
    arg = re.sub(r"^-p\s+", "", arg)
    arg = arg.replace(" ", "")
    if not arg:
        return None, None
    if not _SPEC_RE.match(arg):
        return None, "page spec ভুল"
    return arg, None


def _normalize_spec(spec: str, total: int):
    """Clamp to 1..total, drop empty/reversed parts. Returns (spec_str, count)."""
    pages = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = (int(x) for x in part.split("-"))
            if a > b:
                a, b = b, a
        else:
            a = b = int(part)
        for n in range(max(a, 1), min(b, total) + 1):
            pages.add(n)
    if not pages:
        return None, 0
    return ",".join(str(n) for n in sorted(pages)), len(pages)


async def ocr_pdf(pdf_bytes: bytes, spec: str = None, langs: str = "ben+eng"):
    """Returns (out_bytes, info_dict) or raises RuntimeError(reason)."""
    import fitz  # PyMuPDF
    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
        total = d.page_count
    if spec:
        norm, n_sel = _normalize_spec(spec, total)
        if not norm:
            raise RuntimeError(f"Page range PDF-এর বাইরে (মোট {total} পেজ)")
    else:
        norm, n_sel = None, total

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        dst = os.path.join(tmp, "out.pdf")
        with open(src, "wb") as f:
            f.write(pdf_bytes)
        cmd = ["ocrmypdf", "--redo-ocr", "--optimize", "0",
               "--output-type", "pdf", "-l", langs,
               "--jobs", str(min(4, os.cpu_count() or 2)),
               "--tesseract-timeout", "120"]
        if norm:
            cmd += ["--pages", norm]
        cmd += [src, dst]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=1800)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("OCR সময়সীমা (৩০ মিনিট) পার হয়েছে")
        if proc.returncode != 0 or not os.path.exists(dst):
            msg = (err or b"").decode("utf-8", "ignore").strip().splitlines()
            raise RuntimeError((msg[-1] if msg else f"ocrmypdf exit {proc.returncode}")[:200])
        with open(dst, "rb") as f:
            out = f.read()
    return out, {"total": total, "selected": n_sel, "spec": norm}
