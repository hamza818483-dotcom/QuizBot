# /crop — reply to a PDF: every page is cropped by a fixed margin
# (top/bottom/left/right, in % of page size) and the cropped PDF is returned.
# Usage: /crop            -> default margins (header/footer/side borders removed)
#        /crop T B L R    -> custom margins in %, e.g. /crop 14 14.4 12.3 12.1
import asyncio
import logging

import fitz  # PyMuPDF

from core import send_msg, send_document, download_tg_file

logger = logging.getLogger("quizbot.crop")

# Default % margins (top, bottom, left, right) measured from the sample page.
DEFAULT_MARGINS = (14.0, 14.4, 12.3, 12.1)


def crop_pdf_bytes(data: bytes, t: float, b: float, l: float, r: float) -> bytes:
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        for page in doc:
            pr = page.rect  # rotation-aware visible rect
            w, h = pr.width, pr.height
            rect = fitz.Rect(
                pr.x0 + w * l / 100, pr.y0 + h * t / 100,
                pr.x1 - w * r / 100, pr.y1 - h * b / 100,
            )
            rect = (rect * page.derotation_matrix).normalize()
            page.set_cropbox(rect)
        return doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()


def _parse_margins(text: str):
    parts = text.split()[1:]
    if not parts:
        return DEFAULT_MARGINS
    if len(parts) != 4:
        raise ValueError("4টা সংখ্যা দাও: top bottom left right")
    vals = tuple(float(x.rstrip("%")) for x in parts)
    if any(v < 0 for v in vals) or vals[0] + vals[1] >= 90 or vals[2] + vals[3] >= 90:
        raise ValueError("মান ভুল — অনেক বেশি crop")
    return vals


async def handle_crop_command(msg: dict):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    reply = msg.get("reply_to_message")
    doc = (reply or {}).get("document")
    if not doc:
        await send_msg(chat_id,
            "❌ PDF-এ reply করে /crop দাও\n\n"
            "<code>/crop</code> → default margin\n"
            "<code>/crop T B L R</code> → % এ margin (উপর নিচে বাম ডান)")
        return
    try:
        t, b, l, r = _parse_margins(text)
    except ValueError as e:
        await send_msg(chat_id, f"❌ {e}")
        return
    await send_msg(chat_id, "✂️ Crop হচ্ছে...")
    try:
        data = await download_tg_file(
            doc["file_id"], chat_id=chat_id, message_id=reply["message_id"])
        out = await asyncio.to_thread(crop_pdf_bytes, data, t, b, l, r)
        name = (doc.get("file_name") or "file.pdf")
        if name.lower().endswith(".pdf"):
            name = name[:-4]
        await send_document(
            chat_id, out, f"{name}_cropped.pdf",
            caption=f"✅ Cropped\nT {t}% · B {b}% · L {l}% · R {r}%",
            mime_type="application/pdf")
    except Exception as e:
        logger.exception("crop failed")
        await send_msg(chat_id, f"❌ Crop ব্যর্থ: {e}")
