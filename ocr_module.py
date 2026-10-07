"""/ocr -- make a PDF searchable (invisible text layer) via OCRmyPDF + Tesseract.

Free, no API keys. Existing text is NEVER touched:
  --redo-ocr    keeps all existing real text; additionally OCRs text that lives in
                images/scans on ANY page (mixed pages included)
  --optimize 0  no image recompression
  --output-type pdf  no PDF/A conversion (keeps the file as close to original)
Only the requested pages (--pages) are OCR'd; other pages pass through untouched.

Language is auto-detected PER PAGE (see _detect_page_langs): pure-Bangla
pages use "ben" only, pure-English pages use "eng" only, mixed pages use
"ben+eng". A single global language was tried before and always broke one
side — "ben"-only mangled English, "ben+eng" mangled Bangla (dictionaries
compete on ambiguous glyphs, e.g. "নগ্নবীজী" -> "AIRE"). Per-page detection
avoids the trade-off entirely.
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


def _detect_page_langs(page_src: str) -> str:
    """Page-ta mostly Bangla naki mostly English/Latin, seta dekhe shothik
    Tesseract language set bebohar kori. Ekta-i language shobshomoy use korle
    hoy Bangla bhange (ben+eng), noyto English bhange (ben-only) — tai
    page-wise detect kora shothik fix, kono ekta-ke permanently chere deya na.
    """
    try:
        import fitz  # PyMuPDF
        with fitz.open(page_src) as d:
            page = d[0]
            text = page.get_text() or ""
    except Exception:
        return "ben+eng"
    bangla_chars = sum(1 for c in text if "\u0980" <= c <= "\u09FF")
    latin_chars = sum(1 for c in text if c.isalpha() and c.isascii())
    # existing text layer thakle (mixed page) seta diye bujhi; na thakle
    # (pure scan) image render kore quick OCR sample niye bujhi.
    if bangla_chars == 0 and latin_chars == 0:
        try:
            import fitz
            import pytesseract
            with fitz.open(page_src) as d:
                pix = d[0].get_pixmap(dpi=150)
                img_bytes = pix.tobytes("png")
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(img_bytes))
            sample = pytesseract.image_to_string(img, lang="ben+eng")[:2000]
            bangla_chars = sum(1 for c in sample if "\u0980" <= c <= "\u09FF")
            latin_chars = sum(1 for c in sample if c.isalpha() and c.isascii())
        except Exception:
            return "ben+eng"
    if bangla_chars == 0 and latin_chars > 0:
        return "eng"          # purely English page — Bangla dictionary thakle English bhangbe
    if latin_chars == 0 and bangla_chars > 0:
        return "ben"          # purely Bangla page — English dictionary thakle Bangla bhangbe
    return "ben+eng"          # mixed page — dutai lagbe


async def _ocr_one_page(src_pdf: str, dst_pdf: str, page_no: int, langs: str):
    """OCR a single-page PDF in place. Raises RuntimeError(reason) on failure."""
    cmd = ["ocrmypdf", "--redo-ocr", "--optimize", "0",
           "--output-type", "pdf", "--pdf-renderer", "sandwich", "-l", langs,
           "--tesseract-timeout", "110",
           # oem 1 = LSTM engine only; psm 3 = fully-automatic page segmentation.
           # Default OCRmyPDF leaves oem/psm at Tesseract's own defaults, which
           # on dense Bengali paragraphs can merge 2-4 adjacent words into a
           # single text run -> that run becomes one selectable/copyable blob
           # instead of each word being separately searchable.
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


async def ocr_pdf(pdf_bytes: bytes, spec: str = None, langs: str = None,
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
            page_langs = langs or _detect_page_langs(page_src)
            await _ocr_one_page(page_src, page_dst, pno, page_langs)
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
