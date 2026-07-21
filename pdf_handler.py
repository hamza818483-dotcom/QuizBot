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
    COOLDOWN_SECONDS = 60

    def __init__(self):
        self.keys = []
        self.current = 0
        self._cooldown_until = {}
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

    def ordered_keys(self):
        """All keys, healthy ones first (skip ones on 429 cooldown)."""
        now = time.time()
        healthy = [k for k in self.keys if self._cooldown_until.get(k, 0) <= now]
        cooling = [k for k in self.keys if self._cooldown_until.get(k, 0) > now]
        return healthy + cooling

    def mark_rate_limited(self, key: str):
        self._cooldown_until[key] = time.time() + self.COOLDOWN_SECONDS

    def mark_healthy(self, key: str):
        self._cooldown_until.pop(key, None)

key_rotator = GeminiKeyRotator()

# ============================================================
# OPENROUTER KEY ROTATION
# ============================================================
class OpenRouterKeyRotator:
    COOLDOWN_SECONDS = 60

    def __init__(self):
        self.keys = []
        self.current = 0
        self._cooldown_until = {}
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

    def ordered_keys(self):
        now = time.time()
        healthy = [k for k in self.keys if self._cooldown_until.get(k, 0) <= now]
        cooling = [k for k in self.keys if self._cooldown_until.get(k, 0) > now]
        return healthy + cooling

    def mark_rate_limited(self, key: str):
        self._cooldown_until[key] = time.time() + self.COOLDOWN_SECONDS

    def mark_healthy(self, key: str):
        self._cooldown_until.pop(key, None)

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
-🟥 বক্স/ছক STYLE তথ্য থাকলে (bordered box, info-card, ছক/সারণির প্রতিটি সেল): প্রতিটি বক্স/ছক থেকে MUST কমপক্ষে ১টি MCQ বানাতে হবে, সর্বোচ্চ ২-৩টি একেবারে তুচ্ছ/খালি বক্স বাদ দেওয়া যাবে — এর বেশি বাদ দেওয়া যাবে না। কোনো বক্সে তথ্য বেশি/ঘন থাকলে সেই একটি বক্স থেকেই একাধিক MCQ (সর্বোচ্চ ১৫টি পর্যন্ত) বানাতে হবে।
-টপিকের নাম,অধ্যায়ের নাম,হেডলাইন,পেইজ সংখ্যা,সেকশনের নাম,"Card 1"/"Card 2" এর মতো navigation/label টেক্সট এসব থেকে MCQ বানাবে না — না প্রশ্নে, না অপশনে। এগুলো শুধু structural/navigation elements, প্রকৃত জ্ঞান/তথ্য না।
-প্রতিটি অপশন অবশ্যই actual factual content হতে হবে (definition, cause, treatment, value, name of a real concept ইত্যাদি) — কখনোই কোনো section heading, card/page label, বা navigation text কোনো option হিসেবে ব্যবহার করা যাবে না
-MUST বানাতে হবে exactly {count} টি MCQ, কম বেশি নয়
-Highest quality MCQ বানাবে
-মাঝে মাঝে একই তথ্যকে উল্টিয়েও প্রশ্ন করবে (যেমন "বাংলাদেশের রাজধানী কোথায়?" এর পাশাপাশি অন্য কোথাও "ঢাকা কোন দেশের রাজধানী?" ধরনের reverse angle প্রশ্নও রাখবে, যেখানে যুক্তিসঙ্গত)
-বারবার একই প্যাটার্নে টপিকের নাম ধরে প্রশ্ন শুরু করবে না (যেমন "X সম্পর্কে কোনটি সঠিক", "X এর গঠন কী" — বারবার একই স্টাইল) — বৈচিত্র্যপূর্ণ প্রশ্ন-গঠন ব্যবহার করবে (direct fact, definition, cause-effect, comparison, fill-in-the-blank, "কোনটি সঠিক নয়" ইত্যাদি মিক্স করে)
-জেনারেট করা MCQ-এর মধ্যে ৩-৫টি এমন হবে যেখানে একটি প্রশ্নেই একাধিক ভিন্ন তথ্য মিক্স করা থাকবে (যেমন option-গুলো ২-৩টা তথ্যের কম্বিনেশন, শুধু একটা option-ই সব তথ্য মিলিয়ে সঠিক) — মাঝারি কঠিন রাখবে, extreme hard না

🚫 FORBIDDEN SOURCE-REFERENCE PHRASES (প্রশ্ন ও ব্যাখ্যা দুই জায়গাতেই, সবসময়):
"টেক্সট অনুসারে", "টপিক অনুসারে", "টেক্সটে লিখা আছে", "ছবিতে দেখা যাচ্ছে", "উপরের তথ্য অনুযায়ী", "উক্ত অংশে উল্লেখ আছে", "টপিকে বলা হয়েছে", "দেখা যাচ্ছে", "লিখা আছে", "বর্ণিত আছে" — এই ধরনের কোনো source-reference কথা প্রশ্ন কিংবা ব্যাখ্যা কোথাও লেখা যাবে না, সরাসরি fact বলবে।

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
-🟥 বক্স/ছক STYLE তথ্য থাকলে (bordered box, info-card, ছক/সারণির প্রতিটি সেল): প্রতিটি বক্স/ছক থেকে MUST কমপক্ষে ১টি MCQ বানাতে হবে, সর্বোচ্চ ২-৩টি একেবারে তুচ্ছ/খালি বক্স বাদ দেওয়া যাবে — এর বেশি বাদ দেওয়া যাবে না। কোনো বক্সে তথ্য বেশি/ঘন থাকলে সেই একটি বক্স থেকেই একাধিক MCQ (সর্বোচ্চ ১৫টি পর্যন্ত) বানাতে হবে।
-টপিকের নাম,অধ্যায়ের নাম,হেডলাইন,পেইজ সংখ্যা,সেকশনের নাম,"Card 1"/"Card 2" এর মতো navigation/label টেক্সট এসব থেকে MCQ বানাবে না — না প্রশ্নে, না অপশনে। এগুলো শুধু structural/navigation elements, প্রকৃত জ্ঞান/তথ্য না।
-প্রতিটি অপশন অবশ্যই actual factual content হতে হবে (definition, cause, treatment, value, name of a real concept ইত্যাদি) — কখনোই কোনো section heading, card/page label, বা navigation text কোনো option হিসেবে ব্যবহার করা যাবে না
-হাবিজাবি MCQ বানানো যাবে না,বেশি প্রশ্ন বানানোর প্রয়োজনে একটি MCQ কেই ঘুরিয়ে ফিরিয়ে দেওয়া যেতে পারে
-TARGET কমপক্ষে ১৫টি MCQ বানাবে (default target, user কোনো নির্দিষ্ট সংখ্যা না দিলে) — প্রতিটি লাইন, বক্স, তথ্য, সোর্স ব্যবহার করে; পেইজে তথ্য বেশি থাকলে ৩৫ পর্যন্ত যেতে পারো। "obvious" MCQ শেষ হয়ে গেছে ভেবে ৬-১০টায় থেমে যাওয়া UNDER-EXTRACTING, এটা করা যাবে না।
-তথ্য সত্যিই কম থাকলে minimum 10 টি, একদম sparse হলে minimum 5 টি
-মাঝে মাঝে একই তথ্যকে উল্টিয়েও প্রশ্ন করবে (যেমন "বাংলাদেশের রাজধানী কোথায়?" এর পাশাপাশি অন্য কোথাও "ঢাকা কোন দেশের রাজধানী?" ধরনের reverse angle প্রশ্নও রাখবে, যেখানে যুক্তিসঙ্গত)
-বারবার একই প্যাটার্নে টপিকের নাম ধরে প্রশ্ন শুরু করবে না (যেমন "X সম্পর্কে কোনটি সঠিক", "X এর গঠন কী" — বারবার একই স্টাইল) — বৈচিত্র্যপূর্ণ প্রশ্ন-গঠন ব্যবহার করবে (direct fact, definition, cause-effect, comparison, fill-in-the-blank, "কোনটি সঠিক নয়" ইত্যাদি মিক্স করে)
-জেনারেট করা MCQ-এর মধ্যে ৩-৫টি এমন হবে যেখানে একটি প্রশ্নেই একাধিক ভিন্ন তথ্য মিক্স করা থাকবে (যেমন option-গুলো ২-৩টা তথ্যের কম্বিনেশন, শুধু একটা option-ই সব তথ্য মিলিয়ে সঠিক) — মাঝারি কঠিন রাখবে, extreme hard না

🚫 FORBIDDEN SOURCE-REFERENCE PHRASES (প্রশ্ন ও ব্যাখ্যা দুই জায়গাতেই, সবসময়):
"টেক্সট অনুসারে", "টপিক অনুসারে", "টেক্সটে লিখা আছে", "ছবিতে দেখা যাচ্ছে", "উপরের তথ্য অনুযায়ী", "উক্ত অংশে উল্লেখ আছে", "টপিকে বলা হয়েছে", "দেখা যাচ্ছে", "লিখা আছে", "বর্ণিত আছে" — এই ধরনের কোনো source-reference কথা প্রশ্ন কিংবা ব্যাখ্যা কোথাও লেখা যাবে না, সরাসরি fact বলবে।

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
# v-RAM-fix: pdf2image (poppler) rendering was the biggest RAM spike risk on
# the old 512MB Render instance -- a large PDF at dpi=150 could use 100-300MB
# during conversion, so concurrency was capped hard at 1 and pages/call at 10.
# Now on 16GB HF Space there's ample headroom, so both are raised substantially
# while still keeping some ceiling as a sanity guard against runaway usage.
import threading as _threading
_PDF_CONVERT_LOCK = _threading.Semaphore(6)
_PDF_MAX_PAGES_PER_CALL = 60


def pdf_to_images(pdf_bytes: bytes, page_range: str = None) -> list:
    # Bounded wait (5 min) instead of indefinite block -- avoids thread-pool
    # exhaustion if many uploads queue up at once; caller gets a clear error
    # instead of the request hanging forever.
    if not _PDF_CONVERT_LOCK.acquire(timeout=300):
        raise RuntimeError("PDF conversion queue busy -- try again in a moment")
    try:
        from pdf2image import convert_from_bytes
        def _convert_one_page(p):
            # PERMANENT FIX: a page must never be silently dropped just
            # because a convert_from_bytes call returned empty (transient
            # poppler hiccup, momentary resource blip, etc). 5 attempts with
            # progressive backoff at dpi=150, then a last-ditch dpi=100
            # fallback (some pages fail to render at higher dpi due to
            # memory/complexity but succeed at lower dpi).
            imgs = None
            backoffs = [1, 2, 3, 4]
            for _attempt in range(5):
                try:
                    imgs = convert_from_bytes(pdf_bytes, first_page=p, last_page=p, dpi=150, thread_count=4)
                    if imgs:
                        return imgs[0]
                except Exception as _conv_e:
                    logger.warning(f"[PDF] Page {p} convert attempt {_attempt+1}/5 (dpi=150) raised: {_conv_e}")
                if _attempt < len(backoffs):
                    time.sleep(backoffs[_attempt])
            try:
                imgs = convert_from_bytes(pdf_bytes, first_page=p, last_page=p, dpi=100, thread_count=4)
                if imgs:
                    logger.warning(f"[PDF] Page {p} recovered via dpi=100 fallback after 5 failed dpi=150 attempts")
                    return imgs[0]
            except Exception as _conv_e:
                logger.warning(f"[PDF] Page {p} dpi=100 fallback attempt also raised: {_conv_e}")
            return None

        if page_range:
            parts = page_range.split("-")
            first = int(parts[0])
            last = int(parts[1]) if len(parts) > 1 else first
            if last - first + 1 > _PDF_MAX_PAGES_PER_CALL:
                raise ValueError(
                    f"PDF_RANGE_TOO_LARGE:{first}:{last}:{_PDF_MAX_PAGES_PER_CALL}"
                )
            result = []
            missing_pages = []
            for p in range(first, last + 1):
                img = _convert_one_page(p)
                if img is not None:
                    result.append((p, img))
                else:
                    missing_pages.append(p)
                    logger.error(f"[PDF] Page {p} FAILED to convert after all retries+dpi-fallback — inserting placeholder so page is NEVER skipped/dropped.")
                    result.append((p, Image.new("RGB", (1240, 1754), "white")))
            if missing_pages:
                logger.error(f"[PDF] UNRECOVERABLE render failure (placeholder inserted) for pages: {missing_pages} (out of range {first}-{last})")
            logger.info(f"[PDF] Converted {len(result)} pages")
            return result
        else:
            # REAL BUG FIX: previously an empty convert_from_bytes result on
            # page N was treated as "end of document" via break — but empty
            # can also mean a TRANSIENT failure on a page that is NOT actually
            # the last page, silently truncating the rest of the PDF. Now we
            # retry+dpi-fallback per page, and only treat it as true
            # end-of-document once confirmed against the real page count.
            total_pages = get_pdf_page_count(pdf_bytes)
            result = []
            missing_pages = []
            p = 1
            while p <= _PDF_MAX_PAGES_PER_CALL:
                img = _convert_one_page(p)
                if img is None:
                    if total_pages and p <= total_pages:
                        missing_pages.append(p)
                        logger.error(f"[PDF] Page {p} FAILED to convert after all retries+dpi-fallback (total_pages={total_pages}) — inserting placeholder so page is NEVER skipped/dropped.")
                        result.append((p, Image.new("RGB", (1240, 1754), "white")))
                        p += 1
                        continue
                    else:
                        break  # true end of document (or page count unknown)
                result.append((p, img))
                p += 1
            if p > _PDF_MAX_PAGES_PER_CALL:
                extra = _convert_one_page(p)
                if extra is not None:
                    raise ValueError(f"PDF_TRUNCATED_AT:{_PDF_MAX_PAGES_PER_CALL}")
            if missing_pages:
                logger.error(f"[PDF] UNRECOVERABLE render failure (placeholder inserted) for pages: {missing_pages} (total_pages={total_pages})")
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
def _strip_q_numbering(q: str) -> str:
    """প্রশ্নের শুরুতে numbering prefix (1) 14) 1. Q1. ইত্যাদি) সরায়।"""
    if not q:
        return q
    # NOTE: negative lookahead (?!\d) prevents stripping the leading digit of
    # a decimal number (e.g. "0.05M" must not become "05M").
    pattern = r'^\s*(?:[Qq]\.?\s*)?[\d১২৩৪৫৬৭৮৯০]{1,3}\s*[).।:.\-](?!\d)\s*'
    cur = q
    for _ in range(2):
        new = re.sub(pattern, '', cur)
        if new == cur:
            break
        cur = new
    return cur.strip()

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
                m["question"] = _strip_q_numbering(str(m.get("question", "")))
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
    Single upload (full page, red-border marked at exp_box). Client renders
    a CSS-cropped thumbnail view (object-position) using bbox percentages,
    and full image on click — avoids double-upload fragility.
    Returns {"url": str, "top_pct": float, "bottom_pct": float}.
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
        box_top = (y_min / 1000) * h
        box_bottom = (y_max / 1000) * h

        # Snap top/bottom to nearest blank row so text isn't cut mid-line
        gray = img.convert("L")
        import numpy as np
        arr = np.asarray(gray, dtype=np.uint8)
        row_min = arr.min(axis=1)
        max_extend = int(h * 0.12)

        def _snap_top(y0):
            y = int(y0)
            limit = max(0, y - max_extend)
            i = y
            while i > limit and row_min[i] < 245:
                i -= 1
            return max(0, i)

        def _snap_bottom(y0):
            y = int(y0)
            limit = min(h - 1, y + max_extend)
            i = y
            while i < limit and row_min[i] < 245:
                i += 1
            return min(h, i + 1)

        b_top = max(0, int(box_top))
        b_bottom = min(h, int(box_bottom))

        # Single upload (full page, orange highlight + red box) — avoids the
        # double-upload fragility that was causing MCQs to get skipped on
        # imgbb timeouts/failures.
        full_img = img.convert("RGB").copy()
        if b_bottom > b_top:
            overlay = full_img.copy()
            ov_draw = ImageDraw.Draw(overlay)
            ov_draw.rectangle([0, b_top, w, b_bottom], fill=(255, 165, 0))
            full_img = Image.blend(full_img, overlay, 0.35)
            draw = ImageDraw.Draw(full_img)
            draw.rectangle([6, b_top + 6, w - 6, max(b_top + 7, b_bottom - 6)], outline=(220, 38, 38), width=8)
        url = upload_to_imgbb(image_to_base64(full_img))
        if not url:
            url = upload_to_imgbb(image_to_base64(full_img))  # one retry
        if not url:
            return {}
        return {
            "url": url,
            "top_pct": round((b_top / h) * 100, 2),
            "bottom_pct": round((b_bottom / h) * 100, 2),
        }
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
    max_retries = min(len(openrouter_rotator.keys) * len(OPENROUTER_MODELS), 4)

    for attempt in range(max(max_retries, 3)):
        model = OPENROUTER_MODELS[attempt % len(OPENROUTER_MODELS)]
        try:
            _ordered = openrouter_rotator.ordered_keys()
            key = _ordered[attempt % len(_ordered)] if _ordered else openrouter_rotator.get_key()
            logger.info(f"[OpenRouter] Attempt {attempt+1}, model: {model}")

            async with httpx.AsyncClient(timeout=30) as client:
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
                logger.warning(f"[OpenRouter] Rate limit on attempt {attempt+1}, cooling down key for {openrouter_rotator.COOLDOWN_SECONDS}s, retrying...")
                openrouter_rotator.mark_rate_limited(key)
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
            openrouter_rotator.mark_healthy(key)
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
    """প্রতিটি MCQ-এর exp_bbox থাকলে crop করে upload করে explanation-এ <img> tag জুড়ে দেয়।
    crop_explanation_image() now returns {url, top_pct, bottom_pct} (single-upload,
    CSS-crop design) — must match index.html's renderExpWithImg() which reads
    data-crop-top / data-crop-bottom, NOT the old data-full contract."""
    for m in mcqs:
        bbox = m.get("exp_bbox")
        if not bbox:
            continue
        try:
            result = await asyncio.to_thread(crop_explanation_image, img, bbox)
            url = (result or {}).get("url", "")
            if url:
                exp = m.get("explanation", "") or ""
                top_pct = (result or {}).get("top_pct", 0)
                bot_pct = (result or {}).get("bottom_pct", 100)
                m["explanation"] = (
                    f'{exp} <img src="{url}" data-crop-top="{top_pct}" '
                    f'data-crop-bottom="{bot_pct}">'
                ).strip()
        except Exception as e:
            logger.warning(f"[ExplanationCrop] Attach failed: {e}")
    return mcqs


async def generate_mcq_from_image(
    img: Image.Image,
    topic: str,
    page: int,
    mcq_count: int = None,
) -> list:
    if isinstance(mcq_count, (tuple, list)) and len(mcq_count) == 2:
        c_min, c_max = mcq_count
        range_rule = (
            f"STRICT RANGE REQUIRED: Extract BETWEEN {c_min} AND {c_max} MCQs from "
            f"this page — no fewer than {c_min}, no more than {c_max}. Hard rule, "
            f"not a suggestion."
        )
        prompt = MCQ_PROMPT_WITH_COUNT.format(
            count=f"{c_min}-{c_max} ({range_rule})", topic=topic, page=str(page).zfill(2)
        )
    elif mcq_count:
        prompt = MCQ_PROMPT_WITH_COUNT.format(
            count=mcq_count, topic=topic, page=str(page).zfill(2)
        )
    else:
        prompt = MCQ_PROMPT_MAX.format(topic=topic, page=str(page).zfill(2))

    # ── PRIMARY: Gemini ──────────────────────────────────────
    # v4.5: previously tried EVERY configured key at a full 45s timeout each —
    # with 5-6 keys that's 4-5 minutes of stalling per image before ever
    # reaching the OpenRouter fallback. Cap attempts at 3 keys max, and use a
    # shorter timeout on the 2nd/3rd attempt so a bad/slow key fails fast.
    max_retries = min(len(key_rotator.keys), 3) if key_rotator.keys else 3
    _ordered = key_rotator.ordered_keys()

    for attempt in range(max_retries):
        try:
            key = _ordered[attempt % len(_ordered)] if _ordered else key_rotator.get_key()
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

            _attempt_timeout = 30 if attempt == 0 else 15
            response = await asyncio.wait_for(asyncio.to_thread(_call_gemini), timeout=_attempt_timeout)
            valid = _parse_mcq_json(response.text)
            valid = await _attach_explanation_images(valid, img)
            key_rotator.mark_healthy(key)
            logger.info(f"[Gemini] Page {page}: {len(valid)} MCQs (attempt {attempt+1})")
            return valid

        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                logger.warning(f"[Gemini] Attempt {attempt+1} rate-limited (429), cooling down key for {key_rotator.COOLDOWN_SECONDS}s: {e}")
                key_rotator.mark_rate_limited(key)
            else:
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
2a. এর মধ্যে অন্তত ৩-৫টি MCQ এমন হবে যেখানে একটা single প্রশ্নের মধ্যেই বেশ কয়েকটা আলাদা তথ্য (multiple facts) একসাথে verify করা যায় — যেমন option-গুলো নিজেরাই ২-৩টা তথ্যের combination হবে, আর সঠিক option-টাই একমাত্র যেটা সবগুলো তথ্য মিলিয়ে সঠিক। এগুলো extreme-level কঠিন হবে না — মাঝারি (moderate) কঠিন রাখবে, যাতে মনোযোগ দিয়ে পড়লে বোঝা যায়, কিন্তু অতিরিক্ত ঘুরিয়ে-প্যাঁচিয়ে confusing না হয়।
3. এছাড়াও পুরো text থেকে overall বুঝে কিছু brainstorming MCQ বানাবে — একাধিক তথ্য যুক্তি দিয়ে সংযুক্ত করে গভীর প্রশ্ন (এখনও strictly text-এর তথ্যের ভিত্তিতেই, বাইরের knowledge না)।
3a. এদের মধ্যে কিছু MCQ ইচ্ছাকৃতভাবে "কঠিন/verification-type" হতে হবে — যেগুলো শুধু sample/superficial পড়লে উত্তর দেওয়া যাবে না, বরং পুরো text মনোযোগ দিয়ে ভালোভাবে পড়লেই সঠিক উত্তর দেওয়া সম্ভব হবে (যেমন: দুইটা কাছাকাছি/similar তথ্যের মধ্যে সূক্ষ্ম পার্থক্য ধরিয়ে দেওয়া, ব্যতিক্রম/exception ধরনের তথ্য, একাধিক শর্ত একসাথে মেলানো, বা easily-confused নাম/সংখ্যার মধ্যে সঠিকটা বাছাই)। এগুলো extreme-level কঠিন হবে না, শুধু moderate — এগুলো দিয়ে বোঝা যাবে ইউজার সত্যিই মনোযোগ দিয়ে পুরো text পড়েছে কি না।
4. Explanation-এ সঠিক answer confirm করার পাশাপাশি সংশ্লিষ্ট তথ্যের ঠিক আশেপাশের (আগের/পরের লাইনের) অতিরিক্ত related info যোগ করতে হবে — শুধু answer repeat করা চলবে না। এছাড়া explanation-এ বাকি ৩টা ভুল option কেন ভুল/কী সেটার ছোট ব্যাখ্যাও থাকবে, যাতে ইউজার প্রতিটা option সম্পর্কেই বুঝতে পারে (নিচের rule 4b তে explanation-এর length constraint বিস্তারিত আছে)।
4a. STRICTLY NISHIDDHO (প্রশ্ন এবং explanation দুই জায়গাতেই): "টেক্সট অনুসারে", "টপিক অনুসারে", "টেক্সটে লিখা আছে", "উপরের তথ্য অনুযায়ী", "প্রদত্ত অংশে বলা হয়েছে", "উক্ত অনুচ্ছেদে উল্লেখ আছে", "টপিকে বলা হয়েছে", "দেখা যাচ্ছে", "লিখা আছে", "বর্ণিত আছে" বা এই জাতীয় কোনো source/reference-উল্লেখকারী কথা প্রশ্ন কিংবা explanation কোথাও কখনোই লেখা যাবে না। প্রশ্ন ও explanation দুটোই সরাসরি fact-টুকু বলবে, কোনো source-এর দিকে ইঙ্গিত করবে না।
4b. EXPLANATION LENGTH RULE (Telegram poll explanation box limit মাথায় রেখে): ভুল ৩টা option-এর সংক্ষিপ্ত ব্যাখ্যা অবশ্যই এই box-এর character limit-এর মধ্যে রাখতে হবে (মোট explanation ~200 characters-এর মধ্যে গুছিয়ে রাখার চেষ্টা করবে) — সংক্ষেপে, এক লাইনে বলবে কেন ভুল। সঠিক answer নিয়ে extra info তথ্যবহুল হলে সামান্য limit-এর বাইরে যেতে পারে, কিন্তু চেষ্টা করবে সবটাই যতটা সম্ভব সংক্ষিপ্ত ও গোছানো রাখতে।
5. সঠিক answer (A/B/C/D) প্রতিটি প্রশ্নে ভিন্ন ভিন্ন option-এ থাকতে হবে — কখনোই sequential pattern বা একই option বারবার না।
6. যত ধরনের সম্ভব MCQ variety বানাও — direct fact, definition, cause-effect, comparison, fill-in-the-blank style, "কোনটি সঠিক নয়" ধরনের প্রশ্ন — সব ধরনের প্রশ্ন mix করে বানাও, শুধু এক প্যাটার্নে আটকে থেকো না। প্রশ্ন বানানোর সময় বারবার টপিকের নাম ধরে ধরে প্রশ্ন শুরু করা যাবে না (যেমন "X সম্পর্কে কোনটি সঠিক", "X এর গঠন কী" — একই প্যাটার্নে বারবার) — বৈচিত্র্যপূর্ণ প্রশ্ন-গঠন ব্যবহার করবে, না হলে পড়তে boring লাগে।
7. প্রশ্ন text এর ভাষায় (বাংলা হলে বাংলা, ইংরেজি হলে ইংরেজি)
8. ৪টি option, একটি সঠিক (text থেকে সরাসরি), বাকি ৩টি distractor অবশ্যই text-এর অন্য অংশের প্রকৃত তথ্য/নাম/সংখ্যা থেকে নেওয়া (অন্য লাইনের সত্যিকার তথ্য এখানে ভুল option হিসেবে ব্যবহার করো) — সম্পূর্ণ কল্পনাপ্রসূত/বানানো distractor চলবে না।
8a. OPTION RELEVANCE RULE (must): ৪টি option একই category/type-এর হতে হবে যেমন প্রশ্নের answer-type — যদি প্রশ্নের উত্তর কোনো ব্যক্তি/বিজ্ঞানীর নাম হয়, ৪টি option-ই নাম হবে (এলোমেলো ভিন্ন-টাইপ জিনিস মেশানো যাবে না); যদি উত্তর percentage হয়, ৪টি option-ই percentage হবে; যদি উত্তর সংখ্যা/measurement হয়, ৪টি option-ই একই ধরনের সংখ্যা/measurement হবে। এভাবে option-গুলো দেখলেই বোঝা যাবে প্রশ্নটা আসলে কী নিয়ে, guessing কমে যাবে। বিশেষ ক্ষেত্রে text-এ সেই নির্দিষ্ট category-র পর্যাপ্ত রিলেটেড তথ্য না পাওয়া গেলে, topic-related কাছাকাছি extra info দিয়ে option বানানো যাবে (কিন্তু কখনো সম্পূর্ণ ভিন্ন ধরনের/অপ্রাসঙ্গিক option দেওয়া যাবে না)।
9. Explanation-এর মূল answer-confirm অংশ max 200 chars এর মধ্যে রাখার চেষ্টা করবে (rule 4b অনুযায়ী)।
10. কোনো section heading, "Card 1"/"Card 2", page/chapter label বা navigation text কোনো option হিসেবে ব্যবহার করা যাবে না — প্রতিটি option অবশ্যই actual factual content হতে হবে।
11. STRICTLY NISHIDDHO: প্রশ্নে বা option-এ কখনোই এই ধরনের কথা লেখা যাবে না — "টপিকের নাম কি", "এখানে কি বলা হয়েছে", "প্রদত্ত বর্ণনায় আছে যে", "পাঠ্যবস্তুটির টপিক", "উক্ত অনুচ্ছেদে/টেক্সটে উল্লেখিত", "...কী হিসেবে উল্লেখ করা হয়েছে", "...হিসেবে উল্লেখ করা হয়েছে", বা এই জাতীয় কোনো meta/source-reference কথা। প্রশ্ন সরাসরি বিষয়বস্তু নিয়ে হবে, যেন টেক্সট পড়ে না জানলেও প্রশ্নটা independent একটা knowledge question মনে হয়।
12. Text-এ থাকা যেকোনো #tag, @mention, © copyright line, channel/page/credit name, promotional line — এসব থেকে কোনো MCQ বানানো যাবে না এবং এসব কখনোই question বা option এর content হিসেবে ব্যবহার করা যাবে না।
13. QUESTION/OPTION LENGTH RULE: প্রশ্ন ছোট ও সরাসরি রাখবে (অতিরিক্ত বড় বাক্যের প্রশ্ন এড়িয়ে চলবে)। Option সাধারণত ছোট রাখবে, একটা MCQ-তে সর্বোচ্চ ১-২টা option তুলনামূলক বড় হতে পারে (যদি source fact নিজেই বড় হয়), কিন্তু পুরো ৪টা option বড় বা অতিরিক্ত জটিল বাক্য হওয়া যাবে না। অতিরিক্ত কঠিন/ঘোরানো-প্যাঁচানো বড় প্রশ্ন এড়িয়ে চলবে (rule 3a এর moderate-difficulty MCQ ছাড়া)।
14. MCQ সংখ্যা সাধারণত ১০-২৫টার মধ্যে রাখবে (text-এ তথ্য বেশি থাকলে সর্বোচ্চ ৩৫ পর্যন্ত যেতে পারে) — কিন্তু quantity-র চেয়ে quality সবসময় অগ্রাধিকার পাবে; কম fact থাকা text থেকে জোর করে বেশি MCQ বানানো যাবে না।

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
    _ordered = key_rotator.ordered_keys()
    for attempt in range(max_retries):
        try:
            key = _ordered[attempt % len(_ordered)] if _ordered else key_rotator.get_key()
            from google import genai as gai
            from google.genai import types
            client = gai.Client(api_key=key)

            def _call_gemini():
                return client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[types.Part.from_text(text=prompt)]
                )

            response = await asyncio.wait_for(asyncio.to_thread(_call_gemini), timeout=45)
            valid = _parse_text_json(response.text)
            if valid:
                key_rotator.mark_healthy(key)
                logger.info(f"[Gemini-Text] {len(valid)} MCQs (attempt {attempt+1})")
                return valid
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                key_rotator.mark_rate_limited(key)
                logger.warning(f"[Gemini-Text] Attempt {attempt+1} rate-limited (429), cooling down: {e}")
            else:
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
            _ordered = openrouter_rotator.ordered_keys()
            key = _ordered[attempt % len(_ordered)] if _ordered else openrouter_rotator.get_key()
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
                openrouter_rotator.mark_rate_limited(key)
                await asyncio.sleep(2)
                continue
            if r.status_code != 200:
                continue
            data = r.json()
            raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            valid = _parse_text_json(raw)
            if valid:
                openrouter_rotator.mark_healthy(key)
                logger.info(f"[OpenRouter-Text] {len(valid)} MCQs via {model}")
                return valid
        except Exception as e:
            logger.warning(f"[OpenRouter-Text] {model} attempt {attempt+1} failed: {e}")
            continue

    return []

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
        "mcq_count_min": None,
        "mcq_count_max": None,
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
        # [N-M] রেঞ্জ ব্র্যাকেট: প্রতি পেইজে MCQ সংখ্যা এই রেঞ্জের মধ্যে strictly
        # রাখতে হবে (min-max দুটোই মানতে হবে, কমও না বেশিও না)
        range_match = re.search(r'\[\.?(\d+)\s*-\s*(\d+)\.?\]', text)
        if range_match:
            lo, hi = int(range_match.group(1)), int(range_match.group(2))
            result["mcq_count_min"] = min(lo, hi)
            result["mcq_count_max"] = max(lo, hi)
        else:
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

