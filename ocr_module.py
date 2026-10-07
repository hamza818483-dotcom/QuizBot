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

Orange/red marginal annotations (hand-written codes like "JU-2023",
"RU-21" -- admission-test source tags, not body text) are masked out
(painted white) before OCR, see _mask_orange_annotations: left as-is,
their handwriting-like glyphs get fed into the same OCR pass as the
printed paragraph text and corrupt/merge with nearby real words,
breaking search on the real word next to them (e.g. "দ্বিনিষেক").
Skipping them trades "these codes aren't searchable" for "the real
text next to them stays correct and searchable" -- acceptable per
user request.
"""
import asyncio
import io
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


def _mask_orange_annotations(page_src: str) -> bool:
    """Paint over orange/red-orange handwritten annotation ink (e.g. 'JU-2023',
    'RU-21' marginal codes) with white, directly in the page's embedded images,
    so OCR never sees those glyphs.

    IMPORTANT: this is applied to a throwaway COPY of the page used only to
    produce the invisible text layer (see ocr_pdf's masked_src/clean_src
    split) -- the visible page the user sees keeps its original,
    unmasked image untouched. User-requested: these annotation codes
    should stay visible exactly as-is, they just shouldn't be
    OCR-searchable, since leaving them in the OCR pass corrupts/merges
    with the real Bangla word next to them.

    Mutates page_src in place. Returns True if any image was modified
    (so the caller knows the single-page pdf changed), False on any
    failure or if no orange ink was found (fail-open -- never blocks
    the OCR pass itself).
    """
    try:
        import fitz
        import numpy as np
        import cv2
    except Exception:
        return False
    try:
        changed = False
        d = fitz.open(page_src)
        try:
            page = d[0]
            replacements = []  # (xref, new_png_bytes) collected first, applied after
            for img_info in page.get_images(full=True):
                xref = img_info[0]
                try:
                    base = d.extract_image(xref)
                    img_bytes = base["image"]
                    arr = np.frombuffer(img_bytes, dtype=np.uint8)
                    cv_img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if cv_img is None:
                        continue
                    hsv = cv2.cvtColor(cv_img, cv2.COLOR_BGR2HSV)
                    # Orange/red-orange handwriting ink: narrower + more
                    # saturated range than the general highlighter gate
                    # elsewhere in this codebase -- deliberately avoids
                    # catching pale-yellow highlighter background tint,
                    # only strong orange/red-orange pen/marker strokes.
                    lo = np.array((5, 110, 110))
                    hi = np.array((22, 255, 255))
                    mask = cv2.inRange(hsv, lo, hi)
                    if not mask.any():
                        continue
                    # Dilate slightly so anti-aliased stroke edges are
                    # fully covered, not just the solid-color core.
                    kernel = np.ones((5, 5), np.uint8)
                    mask = cv2.dilate(mask, kernel, iterations=1)
                    cv_img[mask > 0] = (255, 255, 255)
                    # cv_img is BGR; Pixmap wants RGB raw samples, and the
                    # stream must be re-flagged with no filter since it's
                    # now raw pixel data, not the original JPEG/other
                    # encoding -- getting filter/colorspace/size in sync
                    # (via Pixmap+rect replace below) is what update_stream
                    # alone got wrong (left stale DCTDecode tag on new
                    # bytes -> corrupt page).
                    rgb = cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB)
                    replacements.append((xref, rgb, cv_img.shape[1], cv_img.shape[0]))
                except Exception:
                    continue
            # Rebuild each flagged image as a fresh Pixmap and hand it to
            # replace_image, which rewrites the xref's stream together
            # with its Filter/ColorSpace/size metadata in sync -- unlike
            # raw update_stream, which left the old JPEG filter tag on the
            # new raw bytes and produced a corrupt page.
            for xref, rgb, w, h in replacements:
                try:
                    pix = fitz.Pixmap(fitz.csRGB, w, h, rgb.tobytes(), False)
                    # Re-encode as JPEG and pass as `stream=` (raw
                    # compressed bytes), not `pixmap=` -- replace_image's
                    # pixmap= path re-decompresses to raw samples before
                    # storing, which defeats the JPEG encoding and bloats
                    # file size ~40x (the original image was JPEG-encoded).
                    jpeg_bytes = pix.tobytes("jpeg", jpg_quality=90)
                    page.replace_image(xref, stream=jpeg_bytes)
                    changed = True
                except Exception:
                    continue
            if changed:
                out = io.BytesIO()
                d.save(out)
                d.close()
                with open(page_src, "wb") as f:
                    f.write(out.getvalue())
            else:
                d.close()
        except Exception:
            d.close()
            return False
        return changed
    except Exception:
        return False


def _graft_text_layer(ocred_pdf: str, clean_pdf: str, out_pdf: str):
    """Take the invisible OCR text layer from ocred_pdf (produced from a
    masked/throwaway copy of the page) and graft it onto clean_pdf (the
    original, unmasked page -- visible image untouched, including any
    orange annotation ink). Result written to out_pdf.

    ocrmypdf's sandwich renderer puts OCR text as invisible-render-mode
    text spans UNDER/OVER the original page content; we copy the whole
    page's content stream + resources from ocred_pdf onto a fresh copy
    of clean_pdf's page, then re-insert clean_pdf's own image XObject
    so the visible picture is clean_pdf's original (unmasked) one, not
    the masked one baked into ocred_pdf.
    """
    import fitz
    with fitz.open(clean_pdf) as clean_doc, fitz.open(ocred_pdf) as ocred_doc:
        # Simplest robust approach: start from the OCR'd page (has the
        # correct invisible text layer + sandwich structure), then swap
        # its image xref's stream back to the clean/original image bytes
        # -- so the visible picture reverts to unmasked, while the text
        # layer (which doesn't depend on the image bytes, only on the
        # OCR pass that already ran) stays intact.
        clean_page = clean_doc[0]
        ocred_page = ocred_doc[0]
        clean_imgs = clean_page.get_images(full=True)
        ocred_imgs = ocred_page.get_images(full=True)
        if clean_imgs and ocred_imgs:
            clean_xref = clean_imgs[0][0]
            orig_bytes = clean_doc.extract_image(clean_xref)["image"]
            ocred_xref = ocred_imgs[0][0]
            try:
                # replace_image(stream=...) rewrites the xref's stream AND
                # its Filter/ColorSpace/size metadata together in sync --
                # plain update_stream left stale metadata mismatched to
                # the new bytes and produced a corrupt (all-black) page.
                ocred_page.replace_image(ocred_xref, stream=orig_bytes)
            except Exception:
                pass
        ocred_doc.save(out_pdf)


async def _ocr_one_page(src_pdf: str, dst_pdf: str, page_no: int, langs: str):
    """OCR a single-page PDF in place. Raises RuntimeError(reason) on failure."""
    cmd = ["ocrmypdf", "--redo-ocr", "--optimize", "0",
           "--output-type", "pdf", "--pdf-renderer", "sandwich", "-l", langs,
           "--tesseract-timeout", "110",
           # oem 1 = LSTM engine only.
           # psm 4 = "single column of variable-sized text" — recognizes
           # column/line breaks (so English headings, boxed diagram labels,
           # and marginal annotations don't get merged into neighboring
           # Bangla paragraphs mid-word, which previously corrupted English
           # words like "ANGIOSPERMS" into garbage when psm 6 forced the
           # whole mixed-layout page into one block) while still reading
           # each line left-to-right in order (so full words stay intact/
           # searchable, unlike psm 3 which mis-detects word/line boundaries
           # on dense Bengali paragraphs and cuts words like "ডেভোনিয়ান" ->
           # "ডেভো").
           "--tesseract-oem", "1", "--tesseract-pagesegmode", "4",
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
            page_clean = os.path.join(tmp, f"p{pno}_clean.pdf")   # kept unmasked -- this is what gets OCR'd for text, then its image is restored
            page_masked = os.path.join(tmp, f"p{pno}_masked.pdf") # throwaway, orange ink painted white before OCR
            page_dst = os.path.join(tmp, f"p{pno}_out.pdf")       # ocrmypdf output on the masked copy
            page_final = os.path.join(tmp, f"p{pno}_final.pdf")   # OCR'd text layer + original unmasked image
            with fitz.open(stream=pdf_bytes, filetype="pdf") as d1:
                single = fitz.open()
                single.insert_pdf(d1, from_page=pno - 1, to_page=pno - 1)
                single.save(page_clean)
                single.close()
            page_langs = langs or _detect_page_langs(page_clean)
            with fitz.open(stream=pdf_bytes, filetype="pdf") as d2:
                single2 = fitz.open()
                single2.insert_pdf(d2, from_page=pno - 1, to_page=pno - 1)
                single2.save(page_masked)
                single2.close()
            _mask_orange_annotations(page_masked)  # mutates page_masked only; page_clean stays untouched
            await _ocr_one_page(page_masked, page_dst, pno, page_langs)
            _graft_text_layer(page_dst, page_clean, page_final)
            with fitz.open(page_final) as ocred:
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
