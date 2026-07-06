# ============================================================
# ATLAS BOT — PDF Handler
# PDF → Images → Gemini MCQ Generation + OpenRouter Fallback
# ============================================================

import os
import re
import json
import logging
import random
import base64
import asyncio
import time
from io import BytesIO
from PIL import Image

import httpx

logger = logging.getLogger("atlas.pdf_handler")

# ============================================================
# GEMINI KEY ROTATION
# ============================================================
class GeminiKeyRotator:
    def __init__(self):
        self.keys = []
        self.current = 0
        self._load_keys()

    def _load_keys(self):
        raw = os.environ.get("GEMINI_KEYS", "")
        if raw:
            self.keys = [k.strip() for k in raw.split(",") if k.strip()]
        logger.info(f"[Gemini] Loaded {len(self.keys)} keys")

    def get_key(self):
        if not self.keys:
            raise ValueError("No Gemini keys available")
        key = self.keys[self.current % len(self.keys)]
        self.current = (self.current + 1) % len(self.keys)
        return key

key_rotator = GeminiKeyRotator()

# ============================================================
# OPENROUTER KEY ROTATION
# ============================================================
class OpenRouterKeyRotator:
    def __init__(self):
        self.keys = []
        self.current = 0
        self._load_keys()

    def _load_keys(self):
        raw = os.environ.get("OPENROUTER_KEYS", "")
        if raw:
            self.keys = [k.strip() for k in raw.split(",") if k.strip()]
        logger.info(f"[OpenRouter] Loaded {len(self.keys)} keys")

    def get_key(self):
        if not self.keys:
            raise ValueError("No OpenRouter keys available")
        key = self.keys[self.current % len(self.keys)]
        self.current = (self.current + 1) % len(self.keys)
        return key

    def has_keys(self) -> bool:
        return len(self.keys) > 0

openrouter_rotator = OpenRouterKeyRotator()

# ============================================================
# MCQ GENERATION PROMPTS
# ============================================================
MCQ_PROMPT_WITH_COUNT = """📝 Special MCQ TYPE: Standard Easy

🟥Overall Instructions:
-Image এ আগে থেকে MCQ বানানো থাকুক বা Information থাকুক,সকল জায়গা থেকেই প্রশ্ন বানাবে
-যেসব লাইন থেকে MCQ বানানো MISS করা যাবে না (MUST PRIORITY):
  • কোনো পেইজ/লাইন যেকোনো কালার দিয়ে দাগানো বা হাইলাইটেড থাকলে (সবুজ, লাল, কমলা, হলুদ — সবচেয়ে কমন হাইলাইটার কালার)
  • কোনো প্যারা/লাইন বক্স করা থাকলে বা কালার দিয়ে মার্ক করা থাকলে
  • কোনো লাইনের নিচে কলমের কালি দিয়ে আন্ডারলাইন করা থাকলে (লাল, কালো, নীল, সবুজ — যেকোনো কালার)
  • বইয়ের মূল লাইনের সাথে হাতে/কলমে এক্সট্রা কোনো কালার, দাগ, মার্ক, আন্ডারলাইন দেখা গেলেই MUST তা থেকে MCQ বানাতে হবে, মিস করা যাবে না
-কোয়ালিটিফুল প্রশ্ন বানাতে হবে
-ছক থাকলে স্পেশাল প্রায়োরিটি পাবে (Use Every Information for Making MCQ)
-টপিকের নাম,অধ্যায়ের নাম,হেডলাইন,পেইজ সংখ্যা,সেকশনের নাম,"Card 1"/"Card 2" এর মতো navigation/label টেক্সট এসব থেকে MCQ বানাবে না — না প্রশ্নে, না অপশনে। এগুলো শুধু structural/navigation elements, প্রকৃত জ্ঞান/তথ্য না।
-প্রতিটি অপশন অবশ্যই actual factual content হতে হবে (definition, cause, treatment, value, name of a real concept ইত্যাদি) — কখনোই কোনো section heading, card/page label, বা navigation text কোনো option হিসেবে ব্যবহার করা যাবে না
-MUST বানাতে হবে exactly {count} টি MCQ, কম বেশি নয়
-Highest quality MCQ বানাবে

🌐 LANGUAGE RULE (STRICT — MUST FOLLOW):
-Source image-এর মূল ভাষা যা থাকবে (Bengali বা English), Question + Options + Explanation সবকিছু সেই একই ভাষায় লিখতে হবে
-Source ইংরেজি হলে পুরো MCQ ইংরেজিতে লিখবে — বাংলায় translate করা সম্পূর্ণ নিষেধ
-Source বাংলা হলে পুরো MCQ বাংলায় লিখবে — ইংরেজিতে translate করা সম্পূর্ণ নিষেধ
-Mixed-language source হলে, যে অংশ থেকে প্রশ্ন বানাচ্ছো সেই অংশের ভাষা অনুসরণ করবে

💥প্রশ্ন: (ছোট, ১/১.৫/২ লাইন)
💥অপশন: (৪টি, ছোট+মিক্সড সোর্স থেকে)
-অপশনে সঠিক উত্তর অবশ্যই একটিই থাকবে
-৪টি অপশনই তথ্য দ্বারা পরিপূর্ণ থাকবে। হ্যাঁ,না,সত্য,মিথ্যা থাকবে না
💥উত্তর: A/B/C/D — MUST be distributed across different options. STRICTLY FORBIDDEN: all answers being "A" or same option. Each MCQ's correct answer MUST be placed at a different position (A, B, C, or D) — vary them naturally across questions.
💥ব্যাখ্যা (STRICT — MUST FOLLOW): শুধু সঠিক উত্তর কেন সঠিক তা বললেই হবে না — উত্তর + বাকি ৩টি ভুল অপশন সম্পর্কিত অতিরিক্ত তথ্য মিলিয়ে মোট ৪-৫ লাইনের একটি সম্পূর্ণ তথ্যবহুল ব্যাখ্যা লিখতে হবে। এই তথ্য অবশ্যই source image-এর মধ্যেই থাকা কনটেন্ট থেকে নিতে হবে (image-এ যা নেই তা বানিয়ে লেখা যাবে না)। প্রতিটি অপশন নিয়ে সংক্ষেপে বলবে কেনো সেটি সঠিক/ভুল, যাতে পুরো প্রশ্নের বিষয়টি সম্পর্কে একটি সম্পূর্ণ ধারণা পাওয়া যায়। ভাষা source-এর ভাষায় (উপরের LANGUAGE RULE অনুযায়ী)। STRICTLY NISHIDDHO: "টেক্সট অনুসারে", "টপিক অনুসারে", "ছবিতে দেখা যাচ্ছে", "উপরের তথ্য অনুযায়ী", "উক্ত অংশে উল্লেখ আছে" — এমন কোনো source-reference কথা explanation-এ লেখা যাবে না, সরাসরি fact বলবে।
💥exp_bbox: যদি ব্যাখ্যার প্রমাণ সরাসরি image-এর কোনো নির্দিষ্ট অংশে (প্যারাগ্রাফ/লাইন/ছক) visible থাকে, সেই অংশের bounding box দাও [x_min,y_min,x_max,y_max] হিসেবে, image-এর প্রস্থ/উচ্চতার 0-1000 scale-এ normalize করে। প্রমাণ visible না থাকলে বা নিশ্চিত না হলে null দাও।

Topic: {topic}
Page: {page}

MUST Return ONLY valid JSON array, no markdown:
[{{"question":"...","options":["option1","option2","option3","option4"],"answer":"B","explanation":"...","exp_bbox":[100,200,900,350]}}]"""

MCQ_PROMPT_MAX = """📝 Special MCQ TYPE: Standard Easy

🟥Overall Instructions:
-Image এ আগে থেকে MCQ বানানো থাকুক বা Information থাকুক,সকল জায়গা থেকেই প্রশ্ন বানাবে
-যেসব লাইন থেকে MCQ বানানো MISS করা যাবে না (MUST PRIORITY):
  • কোনো পেইজ/লাইন যেকোনো কালার দিয়ে দাগানো বা হাইলাইটেড থাকলে (সবুজ, লাল, কমলা, হলুদ — সবচেয়ে কমন হাইলাইটার কালার)
  • কোনো প্যারা/লাইন বক্স করা থাকলে বা কালার দিয়ে মার্ক করা থাকলে
  • কোনো লাইনের নিচে কলমের কালি দিয়ে আন্ডারলাইন করা থাকলে (লাল, কালো, নীল, সবুজ — যেকোনো কালার)
  • বইয়ের মূল লাইনের সাথে হাতে/কলমে এক্সট্রা কোনো কালার, দাগ, মার্ক, আন্ডারলাইন দেখা গেলেই MUST তা থেকে MCQ বানাতে হবে, মিস করা যাবে না
-কোয়ালিটিফুল প্রশ্ন বানাতে হবে
-এমনভাবে সকল প্রশ্ন বানাবে যাতে সকল লাইন থেকে MCQ কিভাবে আসতে পারে আইডিয়া হয়ে যাবে
-ছক থাকলে স্পেশাল প্রায়োরিটি পাবে (Use Every Information for Making MCQ)
-টপিকের নাম,অধ্যায়ের নাম,হেডলাইন,পেইজ সংখ্যা,সেকশনের নাম,"Card 1"/"Card 2" এর মতো navigation/label টেক্সট এসব থেকে MCQ বানাবে না — না প্রশ্নে, না অপশনে। এগুলো শুধু structural/navigation elements, প্রকৃত জ্ঞান/তথ্য না।
-প্রতিটি অপশন অবশ্যই actual factual content হতে হবে (definition, cause, treatment, value, name of a real concept ইত্যাদি) — কখনোই কোনো section heading, card/page label, বা navigation text কোনো option হিসেবে ব্যবহার করা যাবে না
-হাবিজাবি MCQ বানানো যাবে না,বেশি প্রশ্ন বানানোর প্রয়োজনে একটি MCQ কেই ঘুরিয়ে ফিরিয়ে দেওয়া যেতে পারে
-MAXIMUM possible MCQ বানাবে — প্রতিটি লাইন, বক্স, তথ্য, সোর্স use করে
-তথ্য কম থাকলে minimum 10 টি

🌐 LANGUAGE RULE (STRICT — MUST FOLLOW):
-Source image-এর মূল ভাষা যা থাকবে (Bengali বা English), Question + Options + Explanation সবকিছু সেই একই ভাষায় লিখতে হবে
-Source ইংরেজি হলে পুরো MCQ ইংরেজিতে লিখবে — বাংলায় translate করা সম্পূর্ণ নিষেধ
-Source বাংলা হলে পুরো MCQ বাংলায় লিখবে — ইংরেজিতে translate করা সম্পূর্ণ নিষেধ
-Mixed-language source হলে, যে অংশ থেকে প্রশ্ন বানাচ্ছো সেই অংশের ভাষা অনুসরণ করবে

💥প্রশ্ন: (ছোট, ১/১.৫/২ লাইন)
-সোর্স থেকে সকল টাইপের প্রশ্ন
-যতভাবে প্রশ্ন আসতে পারে সব বানাবে
💥অপশন: (৪টি, ছোট+20% বড়, মিক্সড সোর্স)
-অপশনে সঠিক উত্তর একটিই
-৪টি অপশনই তথ্য দ্বারা পরিপূর্ণ। হ্যাঁ,না,সত্য,মিথ্যা থাকবে না
💥উত্তর: A/B/C/D — MUST be distributed across different options. STRICTLY FORBIDDEN: all answers being "A" or same option. Each MCQ's correct answer MUST be placed at a different position — vary them naturally so answers are spread across A, B, C, D positions.
💥ব্যাখ্যা (STRICT — MUST FOLLOW): শুধু সঠিক উত্তর কেন সঠিক তা বললেই হবে না — উত্তর + বাকি ৩টি ভুল অপশন সম্পর্কিত অতিরিক্ত তথ্য মিলিয়ে মোট ৪-৫ লাইনের একটি সম্পূর্ণ তথ্যবহুল ব্যাখ্যা লিখতে হবে। এই তথ্য অবশ্যই source image-এর মধ্যেই থাকা কনটেন্ট থেকে নিতে হবে (image-এ যা নেই তা বানিয়ে লেখা যাবে না)। প্রতিটি অপশন নিয়ে সংক্ষেপে বলবে কেনো সেটি সঠিক/ভুল, যাতে পুরো প্রশ্নের বিষয়টি সম্পর্কে একটি সম্পূর্ণ ধারণা পাওয়া যায়। ভাষা source-এর ভাষায় (উপরের LANGUAGE RULE অনুযায়ী)। STRICTLY NISHIDDHO: "টেক্সট অনুসারে", "টপিক অনুসারে", "ছবিতে দেখা যাচ্ছে", "উপরের তথ্য অনুযায়ী", "উক্ত অংশে উল্লেখ আছে" — এমন কোনো source-reference কথা explanation-এ লেখা যাবে না, সরাসরি fact বলবে।
💥exp_bbox: যদি ব্যাখ্যার প্রমাণ সরাসরি image-এর কোনো নির্দিষ্ট অংশে (প্যারাগ্রাফ/লাইন/ছক) visible থাকে, সেই অংশের bounding box দাও [x_min,y_min,x_max,y_max] হিসেবে, image-এর প্রস্থ/উচ্চতার 0-1000 scale-এ normalize করে। প্রমাণ visible না থাকলে বা নিশ্চিত না হলে null দাও।

Topic: {topic}
Page: {page}

MUST Return ONLY valid JSON array, no markdown:
[{{"question":"...","options":["option1","option2","option3","option4"],"answer":"C","explanation":"...","exp_bbox":[100,200,900,350]}}]"""

# ============================================================
# PDF TO IMAGES
# ============================================================
# v-RAM-fix: pdf2image (poppler) rendering is the single biggest RAM spike
# risk on a 512MB Render instance -- a large PDF at dpi=150 can use 100-300MB
# during conversion. If two users' /qbm or /pdf uploads convert at the same
# time, RAM can spike well past the limit and get OOM-killed. This semaphore
# limits concurrent conversions -- now that page count per call is hard-capped
# at 60 (below), 2 simultaneous conversions is still safe headroom-wise while
# cutting queue wait roughly in half under 100-concurrent-user load.
import threading as _threading
_PDF_CONVERT_LOCK = _threading.Semaphore(2)
_PDF_MAX_PAGES_PER_CALL = 40


def pdf_to_images(pdf_bytes: bytes, page_range: str = None) -> list:
    # Bounded wait (5 min) instead of indefinite block -- avoids thread-pool
    # exhaustion if many uploads queue up at once; caller gets a clear error
    # instead of the request hanging forever.
    if not _PDF_CONVERT_LOCK.acquire(timeout=300):
        raise RuntimeError("PDF conversion queue busy -- try again in a moment")
    try:
        from pdf2image import convert_from_bytes
        if page_range:
            parts = page_range.split("-")
            first = int(parts[0])
            last = int(parts[1]) if len(parts) > 1 else first
            if last - first + 1 > _PDF_MAX_PAGES_PER_CALL:
                raise ValueError(
                    f"PDF_RANGE_TOO_LARGE:{first}:{last}:{_PDF_MAX_PAGES_PER_CALL}"
                )
            result = []
            for p in range(first, last + 1):
                imgs = convert_from_bytes(pdf_bytes, first_page=p, last_page=p, dpi=150, thread_count=1)
                if imgs:
                    result.append((p, imgs[0]))
            logger.info(f"[PDF] Converted {len(result)} pages")
            return result
        else:
            result = []
            p = 1
            while p <= _PDF_MAX_PAGES_PER_CALL:
                imgs = convert_from_bytes(pdf_bytes, first_page=p, last_page=p, dpi=150, thread_count=1)
                if not imgs:
                    break
                result.append((p, imgs[0]))
                p += 1
            if p > _PDF_MAX_PAGES_PER_CALL:
                extra = convert_from_bytes(pdf_bytes, first_page=p, last_page=p, dpi=150, thread_count=1)
                if extra:
                    raise ValueError(f"PDF_TRUNCATED_AT:{_PDF_MAX_PAGES_PER_CALL}")
            logger.info(f"[PDF] Converted {len(result)} pages")
            return result
    except Exception as e:
        logger.error(f"[PDF] Convert error: {e}")
        raise
    finally:
        _PDF_CONVERT_LOCK.release()


def get_pdf_page_count(pdf_bytes: bytes) -> int:
    """Lightweight page count (no rasterization, minimal RAM) — used for auto-chunking."""
    try:
        from pypdf import PdfReader
        return len(PdfReader(BytesIO(pdf_bytes)).pages)
    except Exception as e:
        logger.warning(f"[PDF] page count failed: {e}")
        return 0

def pdf_to_images_safe(pdf_bytes: bytes, page_range: str = None):
    """Wrapper for pdf_to_images() that turns the RAM-safety exceptions into
    a friendly (ok: bool, result) tuple instead of a raw crash/traceback to
    the user -- result is the page list on success, or a Bengali user-facing
    error string on failure (queue busy / PDF too large / range too large)."""
    try:
        return True, pdf_to_images(pdf_bytes, page_range)
    except ValueError as e:
        msg = str(e)
        if msg.startswith("PDF_TRUNCATED_AT:"):
            cap = msg.split(":")[1]
            return False, (f"❌ PDF-টি {cap} page-এর বেশি! RAM safety-র জন্য একসাথে সর্বোচ্চ "
                            f"{cap} page process করা যায়।\nদয়া করে page range দিয়ে ভাগ করে পাঠাও "
                            f"(যেমন: pages 1-{cap}, তারপর {int(cap)+1}-{int(cap)*2})।")
        if msg.startswith("PDF_RANGE_TOO_LARGE:"):
            _, first, last, cap = msg.split(":")
            return False, (f"❌ এই range-এ {int(last)-int(first)+1} page, কিন্তু সর্বোচ্চ {cap} page "
                            f"একসাথে process করা যায়।\nদয়া করে ছোট range দিয়ে আবার চেষ্টা করো।")
        return False, f"❌ PDF process করতে সমস্যা হয়েছে: {msg}"
    except RuntimeError:
        return False, "⏳ Server এখন busy (অন্য একটা PDF process হচ্ছে), কিছুক্ষণ পর আবার চেষ্টা করো।"
    except Exception as e:
        logger.error(f"[PDF] pdf_to_images_safe unexpected error: {e}")
        return False, "❌ PDF process করতে সমস্যা হয়েছে।"

# ============================================================
# IMAGE HELPERS
# ============================================================
def image_to_base64(img: Image.Image) -> str:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()

def image_to_bytes(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()

# ============================================================
# JSON PARSE HELPER (shared)
# ============================================================
def _parse_mcq_json(text: str) -> list:
    text = text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    mcqs = json.loads(text)
    if not isinstance(mcqs, list) or len(mcqs) == 0:
        raise ValueError("Empty MCQ list")
    valid = []
    _nav_label_re = re.compile(r'^(card|page|section|chapter|part|topic|slide)\s*\d*$', re.IGNORECASE)
    for m in mcqs:
        if all(k in m for k in ["question", "options", "answer", "explanation"]):
            if len(m["options"]) == 4 and m["answer"] in ["A", "B", "C", "D"]:
                # Defense-in-depth: navigation-label-like options (e.g. "Card 1",
                # "Section 2") indicate the AI leaked page-structure text into the
                # options instead of real content — reject this MCQ entirely.
                if any(_nav_label_re.match(str(o).strip()) for o in m["options"]):
                    logger.warning(f"[MCQ] Rejected — nav-label option detected: {m['options']}")
                    continue
                bbox = m.get("exp_bbox")
                if (isinstance(bbox, list) and len(bbox) == 4
                        and all(isinstance(v, (int, float)) for v in bbox)):
                    m["exp_bbox"] = [max(0, min(1000, int(v))) for v in bbox]
                else:
                    m["exp_bbox"] = None
                valid.append(m)

    # Post-process: answer গুলো সব একই হলে shuffle করো
    import random as _rnd
    if valid:
        answers = [m["answer"] for m in valid]
        # সব answer একই হলে force distribute
        if len(set(answers)) == 1:
            labels = ["A", "B", "C", "D"]
            for i, m in enumerate(valid):
                new_ans_label = labels[i % 4]
                new_ans_idx = labels.index(new_ans_label)
                old_ans_idx = labels.index(m["answer"])
                opts = m["options"][:]
                # correct option swap করো new position এ
                opts[old_ans_idx], opts[new_ans_idx] = opts[new_ans_idx], opts[old_ans_idx]
                m["options"] = opts
                m["answer"] = new_ans_label

    return valid


def crop_explanation_image(img: Image.Image, bbox: list) -> dict:
    """
    Returns {"thumb": url, "full": url}.
    thumb = tight vertical crop (full page width) with red border on exp_box —
            shown inline in the poll/explanation.
    full  = the ENTIRE original page image with the same red border marking
            exactly where the thumb came from — shown when thumb is clicked,
            so the user can see the full surrounding context.
    """
    if not bbox or len(bbox) != 4:
        return {}
    try:
        from atlas_mhtml import upload_to_imgbb
        from PIL import ImageDraw
        w, h = img.size
        x_min, y_min, x_max, y_max = bbox
        box_top = (y_min / 1000) * h
        box_bottom = (y_max / 1000) * h
        context_margin = max(2, round(h * 0.003))
        py = max(0, int(box_top - context_margin))
        bottom = min(h, int(box_bottom + context_margin))
        ph = bottom - py
        if ph < 10:
            return {}

        # Full page with red border marking the source region
        full_img = img.convert("RGB").copy()
        full_draw = ImageDraw.Draw(full_img)
        fb_top = max(0, int(box_top))
        fb_bottom = min(h, int(box_bottom))
        if fb_bottom > fb_top:
            full_draw.rectangle([6, fb_top + 6, w - 6, max(fb_top + 7, fb_bottom - 6)], outline=(220, 38, 38), width=6)
        full_url = upload_to_imgbb(image_to_base64(full_img))

        # Tight thumb crop (same border, cropped to just that region)
        cropped = img.crop((0, py, w, bottom)).convert("RGB")
        draw = ImageDraw.Draw(cropped)
        b_top = max(0, int(box_top - py))
        b_bottom = min(ph, int(box_bottom - py))
        b_h = b_bottom - b_top
        if b_h > 0:
            draw.rectangle([6, b_top + 6, w - 6, max(b_top + 7, b_bottom - 6)], outline=(220, 38, 38), width=6)
        thumb_url = upload_to_imgbb(image_to_base64(cropped))

        return {"thumb": thumb_url, "full": full_url}
    except Exception as e:
        logger.warning(f"[ExplanationCrop] Failed: {e}")
        return {}

# ============================================================
# OPENROUTER FALLBACK — Qwen2.5-VL
# ============================================================
OPENROUTER_MODELS = [
    m.strip() for m in
    os.environ.get("OPENROUTER_MODELS",
        "qwen/qwen2.5-vl-72b-instruct:free,qwen/qwen2.5-vl-32b-instruct:free"
    ).split(",") if m.strip()
]

async def _openrouter_fallback(img: Image.Image, prompt: str, page: int) -> list:
    if not openrouter_rotator.has_keys():
        logger.warning("[OpenRouter] No keys available, skipping fallback")
        return []

    img_b64 = image_to_base64(img)
    max_retries = len(openrouter_rotator.keys) * len(OPENROUTER_MODELS)

    for attempt in range(max(max_retries, 3)):
        model = OPENROUTER_MODELS[attempt % len(OPENROUTER_MODELS)]
        try:
            key = openrouter_rotator.get_key()
            logger.info(f"[OpenRouter] Attempt {attempt+1}, model: {model}")

            async with httpx.AsyncClient(timeout=90) as client:
                r = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "HTTP-Referer": "https://atlascourses.com",
                        "X-Title": "ATLAS MCQ Bot"
                    },
                    json={
                        "model": model,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {
                                    "url": f"data:image/jpeg;base64,{img_b64}"
                                }}
                            ]
                        }],
                        "max_tokens": 4096
                    }
                )

            if r.status_code == 429:
                logger.warning(f"[OpenRouter] Rate limit on attempt {attempt+1}, retrying...")
                await asyncio.sleep(2)
                continue

            if r.status_code != 200:
                logger.warning(f"[OpenRouter] HTTP {r.status_code} on attempt {attempt+1}")
                await asyncio.sleep(1)
                continue

            data = r.json()
            text = data["choices"][0]["message"]["content"]
            valid = _parse_mcq_json(text)
            valid = await _attach_explanation_images(valid, img)
            logger.info(f"[OpenRouter] Page {page}: {len(valid)} MCQs via {model}")
            return valid

        except Exception as e:
            logger.warning(f"[OpenRouter] Attempt {attempt+1} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
            continue

    logger.error(f"[OpenRouter] All attempts failed for page {page}")
    return []

# ============================================================
# GENERATE MCQ FROM IMAGE — Gemini primary + OpenRouter fallback
# ============================================================
async def _attach_explanation_images(mcqs: list, img: Image.Image) -> list:
    """প্রতিটি MCQ-এর exp_bbox থাকলে crop করে upload করে explanation-এ <img> tag জুড়ে দেয়।"""
    for m in mcqs:
        bbox = m.get("exp_bbox")
        if not bbox:
            continue
        try:
            url = await asyncio.to_thread(crop_explanation_image, img, bbox)
            if url:
                exp = m.get("explanation", "") or ""
                m["explanation"] = f'{exp} <img src="{url}">'.strip()
        except Exception as e:
            logger.warning(f"[ExplanationCrop] Attach failed: {e}")
    return mcqs


async def generate_mcq_from_image(
    img: Image.Image,
    topic: str,
    page: int,
    mcq_count: int = None,
) -> list:
    if mcq_count:
        prompt = MCQ_PROMPT_WITH_COUNT.format(
            count=mcq_count, topic=topic, page=str(page).zfill(2)
        )
    else:
        prompt = MCQ_PROMPT_MAX.format(topic=topic, page=str(page).zfill(2))

    # ── PRIMARY: Gemini ──────────────────────────────────────
    max_retries = len(key_rotator.keys) if key_rotator.keys else 3

    for attempt in range(max_retries):
        try:
            key = key_rotator.get_key()
            from google import genai as gai
            from google.genai import types
            client = gai.Client(api_key=key)
            img_b64 = image_to_base64(img)

            def _call_gemini():
                return client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[
                        types.Part.from_text(text=prompt),
                        types.Part.from_bytes(
                            data=base64.b64decode(img_b64),
                            mime_type="image/jpeg"
                        )
                    ]
                )

            response = await asyncio.to_thread(_call_gemini)
            valid = _parse_mcq_json(response.text)
            valid = await _attach_explanation_images(valid, img)
            logger.info(f"[Gemini] Page {page}: {len(valid)} MCQs (attempt {attempt+1})")
            return valid

        except Exception as e:
            logger.warning(f"[Gemini] Attempt {attempt+1} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
            continue

    logger.warning(f"[Gemini] All keys failed for page {page} → trying OpenRouter fallback")

    # ── FALLBACK: OpenRouter Qwen2.5-VL ─────────────────────
    return await _openrouter_fallback(img, prompt, page)


async def generate_mcq_from_text(text: str, topic: str = "MCQ", count: int = 15) -> list:
    """Text থেকে MCQ generate করে — same SDK + multi-key + fallback as generate_mcq_from_image"""
    import json as _json

    prompt = f"""তুমি একজন expert MCQ writer। নিচের text-টি লাইন-বাই-লাইন সম্পূর্ণ পড়ো এবং QUALITY বজায় রেখে MCQ বানাও। সংখ্যা কোনো target না — শর্ত মেনে যতগুলো ভালো MCQ বানানো সম্ভব ঠিক ততগুলোই বানাবে, বেশি দেখানোর জন্য জোর করে কম মানের MCQ বানাবে না।

MANDATORY RULES (কোনোটাই skip করা যাবে না):
0. STRICT SOURCE-ONLY RULE: শুধুমাত্র নিচের TEXT-এ যা লেখা আছে সেখান থেকেই MCQ বানাতে হবে। Text-এ নেই এমন কোনো তথ্য, fact, নাম, সংখ্যা নিজে থেকে বানানো/অনুমান করা সম্পূর্ণ নিষেধ। প্রশ্ন ও option ঘুরিয়ে-পেঁচিয়ে (rephrase করে) লেখা যাবে, কিন্তু অর্থ/তথ্য অবশ্যই মূল text থেকেই আসতে হবে — বাইরের কোনো knowledge ব্যবহার করা যাবে না।
1. MANDATORY: Text-এর প্রতিটি লাইন/তথ্যপূর্ণ vakko থেকে অবশ্যই কমপক্ষে একটি MCQ বানাতে হবে — কোনো লাইন বাদ দেওয়া যাবে না (শুধু pure heading/tag/navigation line ছাড়া, যেগুলোতে কোনো factual তথ্যই নেই)। কোনো লাইন সংক্ষিপ্ত/সাধারণ মনে হলেও সেটা থেকে rephrase/context ব্যবহার করে MCQ বানানোর সর্বোচ্চ চেষ্টা করবে।
2. এরপর কয়েকটা লাইনের তথ্য মিক্স/combine করে additional MCQ বানাবে — যেখানে প্রশ্ন বা option একাধিক লাইনের তথ্য একসাথে ব্যবহার করে (যেমন দুইটা ভিন্ন লাইনের ফ্যাক্ট মিলিয়ে comparison/relation ভিত্তিক প্রশ্ন)।
3. এছাড়াও পুরো text থেকে overall বুঝে কিছু brainstorming MCQ বানাবে — একাধিক তথ্য যুক্তি দিয়ে সংযুক্ত করে গভীর প্রশ্ন (এখনও strictly text-এর তথ্যের ভিত্তিতেই, বাইরের knowledge না)।
3a. এদের মধ্যে কিছু MCQ ইচ্ছাকৃতভাবে "কঠিন/verification-type" হতে হবে — যেগুলো শুধু sample/superficial পড়লে উত্তর দেওয়া যাবে না, বরং পুরো text মনোযোগ দিয়ে ভালোভাবে পড়লেই সঠিক উত্তর দেওয়া সম্ভব হবে (যেমন: দুইটা কাছাকাছি/similar তথ্যের মধ্যে সূক্ষ্ম পার্থক্য ধরিয়ে দেওয়া, ব্যতিক্রম/exception ধরনের তথ্য, একাধিক শর্ত একসাথে মেলানো, বা easily-confused নাম/সংখ্যার মধ্যে সঠিকটা বাছাই)। এগুলো দিয়ে বোঝা যাবে ইউজার সত্যিই মনোযোগ দিয়ে পুরো text পড়েছে কি না।
4. Explanation-এ সঠিক answer confirm করার পাশাপাশি সংশ্লিষ্ট তথ্যের ঠিক আশেপাশের (আগের/পরের লাইনের) অতিরিক্ত related info যোগ করতে হবে — শুধু answer repeat করা চলবে না।
4a. STRICTLY NISHIDDHO (explanation-এ): "টেক্সট অনুসারে", "টপিক অনুসারে", "টেক্সটে লিখা আছে", "উপরের তথ্য অনুযায়ী", "প্রদত্ত অংশে বলা হয়েছে", "উক্ত অনুচ্ছেদে উল্লেখ আছে" বা এই জাতীয় কোনো source/reference-উল্লেখকারী কথা explanation-এ কখনোই লেখা যাবে না। Explanation সরাসরি fact-টুকু বলবে, কোনো source-এর দিকে ইঙ্গিত করবে না।
5. সঠিক answer (A/B/C/D) প্রতিটি প্রশ্নে ভিন্ন ভিন্ন option-এ থাকতে হবে — কখনোই sequential pattern বা একই option বারবার না।
6. যত ধরনের সম্ভব MCQ variety বানাও — direct fact, definition, cause-effect, comparison, fill-in-the-blank style, "কোনটি সঠিক নয়" ধরনের প্রশ্ন — সব ধরনের প্রশ্ন mix করে বানাও, শুধু এক প্যাটার্নে আটকে থেকো না।
7. প্রশ্ন text এর ভাষায় (বাংলা হলে বাংলা, ইংরেজি হলে ইংরেজি)
8. ৪টি option, একটি সঠিক (text থেকে সরাসরি), বাকি ৩টি distractor অবশ্যই text-এর অন্য অংশের প্রকৃত তথ্য/নাম/সংখ্যা থেকে নেওয়া (অন্য লাইনের সত্যিকার তথ্য এখানে ভুল option হিসেবে ব্যবহার করো) — সম্পূর্ণ কল্পনাপ্রসূত/বানানো distractor চলবে না
9. Explanation max 200 chars
10. কোনো section heading, "Card 1"/"Card 2", page/chapter label বা navigation text কোনো option হিসেবে ব্যবহার করা যাবে না — প্রতিটি option অবশ্যই actual factual content হতে হবে
11. STRICTLY NISHIDDHO: প্রশ্নে বা option-এ কখনোই এই ধরনের কথা লেখা যাবে না — "টপিকের নাম কি", "এখানে কি বলা হয়েছে", "প্রদত্ত বর্ণনায় আছে যে", "পাঠ্যবস্তুটির টপিক", "উক্ত অনুচ্ছেদে/টেক্সটে উল্লেখিত", "...কী হিসেবে উল্লেখ করা হয়েছে", "...হিসেবে উল্লেখ করা হয়েছে", বা এই জাতীয় কোনো meta/source-reference কথা। প্রশ্ন সরাসরি বিষয়বস্তু নিয়ে হবে, যেন টেক্সট পড়ে না জানলেও প্রশ্নটা independent একটা knowledge question মনে হয়।
12. Text-এ থাকা যেকোনো #tag, @mention, © copyright line, channel/page/credit name, promotional line — এসব থেকে কোনো MCQ বানানো যাবে না এবং এসব কখনোই question বা option এর content হিসেবে ব্যবহার করা যাবে না।

TEXT:
{text[:4000]}

Return ONLY valid JSON array, no markdown, no extra text:
[{{"question":"...","options":["...","...","...","..."],"answer":"B","explanation":"..."}}]"""

    def _parse_text_json(raw: str) -> list:
        raw = raw.strip()
        if "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()
        try:
            mcqs = _json.loads(raw)
        except Exception:
            return []
        return [m for m in mcqs if all(k in m for k in ["question","options","answer","explanation"])
                and len(m.get("options", [])) >= 4 and m["answer"] in ["A","B","C","D"]
                and not any(re.match(r'^(card|page|section|chapter|part|topic|slide)\s*\d*$', str(o).strip(), re.IGNORECASE) for o in m.get("options", []))]

    # ── PRIMARY: Gemini (new google.genai SDK, multi-key rotation) ──
    max_retries = len(key_rotator.keys) if key_rotator.keys else 3
    for attempt in range(max_retries):
        try:
            key = key_rotator.get_key()
            from google import genai as gai
            from google.genai import types
            client = gai.Client(api_key=key)

            def _call_gemini():
                return client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[types.Part.from_text(text=prompt)]
                )

            response = await asyncio.to_thread(_call_gemini)
            valid = _parse_text_json(response.text)
            if valid:
                logger.info(f"[Gemini-Text] {len(valid)} MCQs (attempt {attempt+1})")
                return valid
        except Exception as e:
            logger.warning(f"[Gemini-Text] Attempt {attempt+1} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
            continue

    logger.warning("[Gemini-Text] All keys failed → trying OpenRouter text fallback")

    # ── FALLBACK: OpenRouter (text-only chat completion) ──
    if not openrouter_rotator.has_keys():
        logger.warning("[OpenRouter-Text] No keys available, skipping fallback")
        return []

    max_or_retries = len(openrouter_rotator.keys) * len(OPENROUTER_MODELS)
    for attempt in range(max(max_or_retries, 3)):
        model = OPENROUTER_MODELS[attempt % len(OPENROUTER_MODELS)]
        try:
            key = openrouter_rotator.get_key()
            async with httpx.AsyncClient(timeout=90) as client:
                r = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "HTTP-Referer": "https://atlascourses.com",
                        "X-Title": "ATLAS MCQ Bot"
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 4096
                    }
                )
            if r.status_code == 429:
                await asyncio.sleep(2)
                continue
            if r.status_code != 200:
                continue
            data = r.json()
            raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            valid = _parse_text_json(raw)
            if valid:
                logger.info(f"[OpenRouter-Text] {len(valid)} MCQs via {model}")
                return valid
        except Exception as e:
            logger.warning(f"[OpenRouter-Text] {model} attempt {attempt+1} failed: {e}")
            continue

    return []


    return await generate_mcq_from_image(img, topic, page, mcq_count=count)

# ============================================================
# PARSE HELPERS
# ============================================================
def parse_page_range(page_range: str) -> tuple:
    try:
        if "-" in page_range:
            parts = page_range.split("-")
            return int(parts[0]), int(parts[1])
        else:
            n = int(page_range)
            return n, n
    except:
        return None, None

def parse_pdf_command(text: str) -> dict:
    import re
    result = {
        "page_range": None,
        "channel_id": None,
        "topic": None,
        "mcq_count": None,
        "thread_id": None
    }
    try:
        p_match = re.search(r'-p\s+([\d\-]+)', text)
        if p_match:
            result["page_range"] = p_match.group(1)
        c_match = re.search(r'-c\s+(\S+)', text)
        if c_match:
            result["channel_id"] = c_match.group(1)
        # -t থ্রেড আইডি: কোটেশন সহ (-t "447") বা ছাড়া (-t 447) দুই ফরম্যাটেই কাজ করবে
        t_match = re.search(r'-t\s+"(\d+)"', text) or re.search(r"-t\s+'(\d+)'", text) or re.search(r'-t\s+(\d+)', text)
        if t_match:
            result["thread_id"] = int(t_match.group(1))
        m_match = re.search(r'-m\s+"([^"]+)"', text)
        if m_match:
            result["topic"] = m_match.group(1)
        else:
            m_match = re.search(r'-m\s+(\S+)', text)
            if m_match:
                result["topic"] = m_match.group(1)
        # [.N.] বা [N] ব্র্যাকেট: প্রতি পেইজে কতগুলো MCQ বানাতে হবে সেটা স্পষ্টভাবে
        # বোঝায় (কমান্ডের শেষে থাকা bare সংখ্যার অস্পষ্ট অনুমানের চেয়ে অগ্রাধিকার পাবে)
        bracket_match = re.search(r'\[\.?(\d+)\.?\]', text)
        if bracket_match:
            result["mcq_count"] = int(bracket_match.group(1))
        else:
            cmd_part = text.split('/pdf')[1] if '/pdf' in text else text
            nums = re.findall(r'(?<!\d)(\d+)(?!\d)', cmd_part)
            if nums:
                last_num = int(nums[-1])
                page_nums = result["page_range"].replace("-", " ").split() if result["page_range"] else []
                if str(last_num) not in page_nums and last_num < 200:
                    result["mcq_count"] = last_num
    except Exception as e:
        logger.error(f"[Parse] PDF command error: {e}")
    return result

def fmt_page(n: int) -> str:
    return str(n).zfill(2)

def gen_session_id() -> str:
    import random, string
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

# ============================================================
# ISLAMIC AYATS + MOTIVATION
# ============================================================
ISLAMIC_AYATS = [
    '"নিশ্চয়ই কষ্টের সাথে স্বস্তি আছে।" (সূরা ইনশিরাহ: ৬)',
    '"আল্লাহ কোনো আত্মার উপর তার সাধ্যের বাইরে বোঝা চাপান না।" (সূরা বাকারা: ২৮৬)',
    '"জ্ঞানীরাই আল্লাহকে বেশি ভয় করে।" (সূরা ফাতির: ২৮)',
    '"তোমরা হতাশ হয়ো না, দুঃখ করো না। তোমরাই বিজয়ী হবে।" (সূরা আল-ইমরান: ১৩৯)',
    '"আল্লাহর রহমত থেকে নিরাশ হয়ো না।" (সূরা যুমার: ৫৩)',
    '"সবর করো, নিশ্চয়ই আল্লাহ সবরকারীদের সাথে আছেন।" (সূরা বাকারা: ১৫৩)',
    '"তোমাদের প্রতিপালক বলেন: আমাকে ডাকো, আমি সাড়া দেব।" (সূরা মুমিন: ৬০)',
    '"যে আল্লাহর উপর ভরসা করে, তার জন্য আল্লাহই যথেষ্ট।" (সূরা তালাক: ৩)',
    '"আল্লাহ তাওবাকারীদের ভালোবাসেন।" (সূরা বাকারা: ২২২)',
    '"প্রতিটি কঠিনতার সাথেই সহজতা রয়েছে।" (সূরা ইনশিরাহ: ৫)',
]

def get_random_ayat() -> str:
    return random.choice(ISLAMIC_AYATS)

def get_motivation(pct: float) -> str:
    if pct >= 90:
        return "🏆 অসাধারণ! তুমি সেরা! আরও এগিয়ে যাও!"
    elif pct >= 70:
        return "🎉 চমৎকার! তুমি খুব ভালো করেছো!"
    elif pct >= 50:
        return "👍 মোটামুটি ভালো! আরও একটু পড়াশোনা করো!"
    else:
        return "📚 পড়া হয়নি! আবার পড়ে চেষ্টা করো!"

generate_new_mcq = generate_mcq_from_image

