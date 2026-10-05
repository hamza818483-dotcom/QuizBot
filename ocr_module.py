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


async def _ocr_one_page(src_pdf: str, dst_pdf: str, page_no: int, langs: str):
    """OCR a single-page PDF in place. Raises RuntimeError(reason) on failure."""
    cmd = ["ocrmypdf", "--redo-ocr", "--optimize", "0",
           "--output-type", "pdf", "--pdf-renderer", "sandwich", "-l", langs,
           "--tesseract-timeout", "110",
           # oem 1 = LSTM engine only; psm 3 = fully-automatic page segmentation
           # (no OSD). Default OCRmyPDF leaves these at Tesseract's own defaults,
           # which on dense Bengali paragraphs can merge 2-4 adjacent words into
           # a single text run -> that whole run becomes one selectable/copyable
           # blob instead of each word being separately searchable/selectable.
           # Forcing oem 1 + psm 3 makes Tesseract emit proper per-word boxes.
           "--tesseract-oem", "1", "--tesseract-pagesegmode", "3",
           src_pdf, dst_pdf]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"পেজ {page_no}: OCR টাইমআউট")
    if proc.returncode != 0 or not os.path.exists(dst_pdf):
        msg = (err or b"").decode("utf-8", "ignore").strip().splitlines()
        raise RuntimeError(f"পেজ {page_no}: " + ((msg[-1] if msg else f"exit {proc.returncode}")[:150]))


async def ocr_pdf(pdf_bytes: bytes, spec: str = None, langs: str = "ben+eng",
                   progress_cb=None):
    """Returns (out_bytes, info_dict) or raises RuntimeError(reason).

    OCRs one page at a time (deterministic progress) and reassembles the
    output PDF via PyMuPDF. progress_cb(done, total) is awaited after each
    page so callers can render a %/elapsed-time dashboard.
    """
    import fitz  # PyMuPDF
    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
        total = d.page_count
    if spec:
        norm, n_sel = _normalize_spec(spec, total)
        if not norm:
            raise RuntimeError(f"Page range PDF-এর বাইরে (মোট {total} পেজ)")
        pages = []
        for part in norm.split(","):
            if "-" in part:
                a, b = (int(x) for x in part.split("-"))
            else:
                a = b = int(part)
            pages.extend(range(a, b + 1))
    else:
        norm, n_sel = None, total
        pages = list(range(1, total + 1))

    with tempfile.TemporaryDirectory() as tmp:
        src_whole = os.path.join(tmp, "in.pdf")
        with open(src_whole, "wb") as f:
            f.write(pdf_bytes)

        out_doc = fitz.open(stream=pdf_bytes, filetype="pdf")  # start as full original
        if progress_cb:
            await progress_cb(0, len(pages))
        for i, pno in enumerate(pages, 1):
            page_src = os.path.join(tmp, f"p{pno}.pdf")
            page_dst = os.path.join(tmp, f"p{pno}_out.pdf")
            with fitz.open(stream=pdf_bytes, filetype="pdf") as d1:
                single = fitz.open()
                single.insert_pdf(d1, from_page=pno - 1, to_page=pno - 1)
                single.save(page_src)
                single.close()
            await _ocr_one_page(page_src, page_dst, pno, langs)
            with fitz.open(page_dst) as ocred:
                out_doc.delete_page(pno - 1)
                out_doc.insert_pdf(ocred, from_page=0, to_page=0, start_at=pno - 1)
            if progress_cb:
                await progress_cb(i, len(pages))

        dst = os.path.join(tmp, "final.pdf")
        out_doc.save(dst)
        out_doc.close()
        with open(dst, "rb") as f:
            out = f.read()
    return out, {"total": total, "selected": n_sel, "spec": norm}
