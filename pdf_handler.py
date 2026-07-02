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
-কোনো টেক্সটের নিচে কালার মার্ক বা কোনো টেক্সট হাইলাইটেড থাকলে সেখান থেকে প্রশ্ন বানানো মিস দেওয়া যাবে না (must priority)
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
💥ব্যাখ্যা: max 200 chars, source-এর ভাষায় (উপরের LANGUAGE RULE অনুযায়ী)

Topic: {topic}
Page: {page}

MUST Return ONLY valid JSON array, no markdown:
[{{"question":"...","options":["option1","option2","option3","option4"],"answer":"B","explanation":"..."}}]"""

MCQ_PROMPT_MAX = """📝 Special MCQ TYPE: Standard Easy

🟥Overall Instructions:
-Image এ আগে থেকে MCQ বানানো থাকুক বা Information থাকুক,সকল জায়গা থেকেই প্রশ্ন বানাবে
-কোনো টেক্সটের নিচে কালার মার্ক বা কোনো টেক্সট হাইলাইটেড থাকলে সেখান থেকে প্রশ্ন বানানো মিস দেওয়া যাবে না (must priority)
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
💥ব্যাখ্যা: max 200 chars, source-এর ভাষায় (উপরের LANGUAGE RULE অনুযায়ী)

Topic: {topic}
Page: {page}

MUST Return ONLY valid JSON array, no markdown:
[{{"question":"...","options":["option1","option2","option3","option4"],"answer":"C","explanation":"..."}}]"""

# ============================================================
# PDF TO IMAGES
# ============================================================
def pdf_to_images(pdf_bytes: bytes, page_range: str = None) -> list:
    try:
        from pdf2image import convert_from_bytes
        if page_range:
            parts = page_range.split("-")
            first = int(parts[0])
            last = int(parts[1]) if len(parts) > 1 else first
            images = convert_from_bytes(pdf_bytes, first_page=first, last_page=last, dpi=150)
            page_numbers = list(range(first, last + 1))
        else:
            images = convert_from_bytes(pdf_bytes, dpi=150)
            page_numbers = list(range(1, len(images) + 1))
        logger.info(f"[PDF] Converted {len(images)} pages")
        return list(zip(page_numbers, images))
    except Exception as e:
        logger.error(f"[PDF] Convert error: {e}")
        raise

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

    prompt = f"""নিচের text থেকে {count}টি MCQ বানাও।

RULES:
- প্রশ্ন text এর ভাষায় (বাংলা হলে বাংলা, ইংরেজি হলে ইংরেজি)
- ৪টি option, একটি সঠিক
- Answer A/B/C/D — MUST vary across questions, NEVER all same
- Explanation max 200 chars
- কোনো section heading, "Card 1"/"Card 2", page/chapter label বা navigation text কোনো option হিসেবে ব্যবহার করা যাবে না — প্রতিটি option অবশ্যই actual factual content হতে হবে

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
        t_match = re.search(r'-t\s+(\d+)', text)
        if t_match:
            result["thread_id"] = int(t_match.group(1))
        m_match = re.search(r'-m\s+"([^"]+)"', text)
        if m_match:
            result["topic"] = m_match.group(1)
        else:
            m_match = re.search(r'-m\s+(\S+)', text)
            if m_match:
                result["topic"] = m_match.group(1)
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

