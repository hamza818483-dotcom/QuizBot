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
import contextvars
import hashlib
from io import BytesIO
from PIL import Image

# Global queue lock: ensures only ONE MCQ-generation job (image or text)
# runs at a time across the entire bot, regardless of which command
# triggered it (/img, /pdf, /csv, quiz-master, etc). Other bot commands
# (menu, settings, admin, etc.) never touch this lock and stay fully parallel.
MCQ_PROCESSING_QUEUE_LOCK = asyncio.Lock()

import httpx

try:
    from core import BD_TZ
except Exception:
    BD_TZ = None

try:
    from zoneinfo import ZoneInfo
    GEMINI_QUOTA_TZ = ZoneInfo("America/Los_Angeles")  # Google free-tier quota resets at Pacific midnight
except Exception:
    GEMINI_QUOTA_TZ = None

logger = logging.getLogger("atlas.pdf_handler")

# ============================================================
# GEMINI KEY ROTATION
# ============================================================
_gemini_key_exhausted_day: dict = {}   # key -> 'YYYY-MM-DD' (Pacific) it was marked exhausted
_gemini_key_exhausted_flag: dict = {}  # key -> True while exhausted-today
_gemini_exhausted_d1_loaded = False    # set True once startup rehydrate has run

def _gemini_key_hash(key: str) -> str:
    """Short, stable, non-reversible identifier for a key -- stored in D1
    instead of the raw key so the exhausted-keys table never holds live
    credentials."""
    return hashlib.sha256(key.encode()).hexdigest()[:24]

def _gemini_quota_today_str() -> str:
    from datetime import datetime
    if GEMINI_QUOTA_TZ is not None:
        return datetime.now(GEMINI_QUOTA_TZ).strftime('%Y-%m-%d')
    return datetime.utcnow().strftime('%Y-%m-%d')

async def load_gemini_exhausted_keys_from_d1():
    """Call once at bot startup, after key_rotator is constructed. Rehydrates
    the in-memory exhausted-today state from D1 so a restart doesn't lose
    it -- without this, /keys always shows every key "healthy" right after
    a restart even if Google's actual daily quota for those keys is still
    exhausted, and the bot wastes a fresh 429 round-trip re-discovering
    what it already knew before restarting."""
    global _gemini_exhausted_d1_loaded
    try:
        from core import db_load_gemini_exhausted_keys
        today = _gemini_quota_today_str()
        rows = await db_load_gemini_exhausted_keys()
        restored = 0
        for key in key_rotator.keys:
            h = _gemini_key_hash(key)
            day = rows.get(h)
            if day == today:
                _gemini_key_exhausted_day[key] = day
                _gemini_key_exhausted_flag[key] = True
                restored += 1
        if restored:
            logger.warning(f"[Gemini] Restored {restored} daily-exhausted key(s) from D1 after restart")
        _gemini_exhausted_d1_loaded = True
    except Exception as e:
        logger.warning(f"[Gemini] load_gemini_exhausted_keys_from_d1 failed (non-fatal, starts fresh): {e}")

def _is_gemini_key_exhausted_today(key: str) -> bool:
    """Daily quota-exhaustion memory for Gemini free-tier keys
    (20 requests/day/model). A 60s cooldown is pointless for a daily quota —
    the key stays dead until the quota resets, so once it 429s with a
    quota/RESOURCE_EXHAUSTED error, skip it until Google's actual reset
    (Pacific midnight) instead of re-trying it every call."""
    today = _gemini_quota_today_str()
    if _gemini_key_exhausted_day.get(key) != today:
        _gemini_key_exhausted_day[key] = today
        _gemini_key_exhausted_flag[key] = False
    return _gemini_key_exhausted_flag.get(key, False)

def _mark_gemini_key_exhausted_today(key: str):
    today = _gemini_quota_today_str()
    _gemini_key_exhausted_day[key] = today
    _gemini_key_exhausted_flag[key] = True
    try:
        import asyncio
        from core import db_mark_gemini_key_exhausted
        asyncio.create_task(db_mark_gemini_key_exhausted(_gemini_key_hash(key), today))
    except Exception as e:
        logger.warning(f"[Gemini] D1 persist exhausted-mark failed (non-fatal, in-memory state still correct): {e}")

def _mark_gemini_key_healthy_today(key: str):
    _gemini_key_exhausted_flag[key] = False
    try:
        import asyncio
        from core import db_mark_gemini_key_healthy
        asyncio.create_task(db_mark_gemini_key_healthy(_gemini_key_hash(key)))
    except Exception as e:
        logger.warning(f"[Gemini] D1 clear exhausted-mark failed (non-fatal, in-memory state still correct): {e}")


_BANNED_KEYS_FILE = "/tmp/atlas_banned_gemini_keys.json"

def _load_banned_keys() -> set:
    try:
        with open(_BANNED_KEYS_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def _save_banned_keys(banned: set):
    try:
        with open(_BANNED_KEYS_FILE, "w") as f:
            json.dump(list(banned), f)
    except Exception as e:
        logger.warning(f"[Gemini] Failed to persist banned keys: {e}")


class GeminiKeyRotator:
    COOLDOWN_SECONDS = 60
    RPM_PER_KEY = 15  # proactive per-minute ceiling; skip a key before it 429s
    RPM_WINDOW_SECONDS = 60

    def __init__(self):
        self.keys = []
        self.current = 0
        self._cooldown_until = {}
        self._banned = _load_banned_keys()
        self._call_times = {}  # key -> list[float] call timestamps (rolling 60s window)
        self._load_keys()


    def _load_keys(self):
        raw = os.environ.get("GEMINI_KEYS", "")
        if raw:
            all_keys = [k.strip() for k in raw.split(",") if k.strip()]
        else:
            all_keys = []
        skipped = [k for k in all_keys if k in self._banned]
        self.keys = [k for k in all_keys if k not in self._banned]
        if skipped:
            logger.warning(f"[Gemini] Skipped {len(skipped)} previously-banned key(s) at startup: {[k[:12]+'...' for k in skipped]}")
        logger.info(f"[Gemini] Loaded {len(self.keys)} usable keys ({len(skipped)} auto-skipped as banned)")

    def _prune_and_count(self, key: str, now: float) -> int:
        """Drops call-timestamps older than the rolling window and returns
        the remaining count for this key."""
        times = self._call_times.get(key)
        if not times:
            return 0
        cutoff = now - self.RPM_WINDOW_SECONDS
        fresh = [t for t in times if t > cutoff]
        self._call_times[key] = fresh
        return len(fresh)

    def record_call(self, key: str):
        """Call this right before/when actually using a key, so the rolling
        window reflects real usage (independent of mark_healthy/rate_limited)."""
        self._call_times.setdefault(key, []).append(time.time())

    def get_key(self):
        if not self.keys:
            raise ValueError("No Gemini keys available")
        key = self.keys[self.current % len(self.keys)]
        self.current = (self.current + 1) % len(self.keys)
        return key

    def ordered_keys(self, offset: int = 0):
        """Only non-banned keys, healthy ones first: not exhausted-today AND
        not in short cooldown AND not over its rolling per-minute ceiling,
        then over-RPM keys, then short-cooldown keys, then today-exhausted
        keys last — so a call never wastes its first attempt on a key
        already known dead for the day (daily quota resets at Pacific midnight,
        not after 60s), or one about to 429 from hitting its per-minute limit.

        Within the healthy tier, starts from self.current (advanced by
        get_key(), also nudged here) instead of always self.keys[0] --
        without this, every single call started its search from the same
        first key, so that key alone absorbed the first attempt of nearly
        every request and burned through its daily quota far faster than
        the other 35, even though total load was fairly distributed
        overall. Round-robining the healthy tier spreads first-attempts
        evenly across all live keys.

        `offset` lets QBM's parallel page-window give each concurrent slot
        its own starting key (same pattern as Groq's ordered_keys(offset=))
        -- without it, N pages running concurrently all read the same
        self.current snapshot before any of their awaits land, so they'd
        all pick the SAME "healthiest" key and hammer it at once instead of
        spreading across the pool, causing avoidable 429s mid-page for
        Gemini specifically (which now runs Call1 + miss-check + verify,
        3x the Gemini load per page QBM had before)."""
        now = time.time()
        live_keys = [k for k in self.keys if k not in self._banned]
        not_exhausted = [k for k in live_keys if not _is_gemini_key_exhausted_today(k)]
        exhausted = [k for k in live_keys if _is_gemini_key_exhausted_today(k)]
        pool = not_exhausted if not_exhausted else live_keys
        cooled = [k for k in pool if self._cooldown_until.get(k, 0) <= now]
        cooling = [k for k in pool if self._cooldown_until.get(k, 0) > now]
        under_rpm = [k for k in cooled if self._prune_and_count(k, now) < self.RPM_PER_KEY]
        over_rpm = [k for k in cooled if self._prune_and_count(k, now) >= self.RPM_PER_KEY]
        healthy = under_rpm
        if healthy:
            start = (self.current + offset) % len(healthy)
            healthy = healthy[start:] + healthy[:start]
            self.current = (self.current + 1) % max(len(self.keys), 1)
        return healthy + over_rpm + cooling + (exhausted if not_exhausted else [])

    def mark_rate_limited(self, key: str, daily_exhausted: bool = False, retry_after_seconds: int = None):
        cooldown = retry_after_seconds if retry_after_seconds and retry_after_seconds > 0 else self.COOLDOWN_SECONDS
        self._cooldown_until[key] = time.time() + cooldown
        if daily_exhausted:
            _mark_gemini_key_exhausted_today(key)

    def mark_banned(self, key: str):
        """Permanently skip this key (e.g. 403 CONSUMER_SUSPENDED / invalid key)
        — removed from the active pool immediately and persisted to disk so
        restarts don't waste a retry cycle on a key that will keep failing
        forever."""
        self._cooldown_until[key] = float("inf")
        self._banned.add(key)
        self.keys = [k for k in self.keys if k != key]
        _save_banned_keys(self._banned)
        logger.error(f"[Gemini] Key {key[:12]}... permanently banned and removed from rotation ({len(self.keys)} keys remain)")

    def mark_healthy(self, key: str):
        self._cooldown_until.pop(key, None)
        _mark_gemini_key_healthy_today(key)

key_rotator = GeminiKeyRotator()

# Shared with app.py's qbm_extract_all_pages: each concurrent page-window
# slot sets this to a distinct offset before calling into any Gemini
# extraction path here, so ordered_keys(offset=...) below spreads
# concurrent Call1/Call2 requests across different starting keys instead
# of every slot racing for the same "healthiest" key and all 429-ing it
# at once (the bug behind "3 keys exhausted but Gemini still fails" --
# many concurrent slots all picked the SAME first-choice key before any
# of their mark_rate_limited() calls could land, so the exhaustion wasn't
# visible to the others in time). contextvars propagate through await
# chains automatically, so app.py setting this before calling into
# pdf_handler.py functions works with zero explicit parameter threading.
_qbm_key_offset_ctx = contextvars.ContextVar("_qbm_key_offset_ctx", default=0)

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
MCQ_PROMPT_WITH_COUNT = """📝 এই page-টা থেকে MCQ বানাও।

🎯 MAIN RULE — MAXIMUM CONTENT USE: পুরো page-এর প্রতিটা অংশ (প্রতিটা প্যারাগ্রাফ, লাইন, বক্স/ছক, হাইলাইট/মার্ক করা অংশ) ভালোভাবে পড়ো এবং যত বেশি সম্ভব actual তথ্য থেকে MCQ বানাও — কোনো তথ্যবহুল অংশ বাদ দেওয়া যাবে না, পুরো page-এর content maximize করে ব্যবহার করবে।

- Page-এ আগে থেকে MCQ (question+options) থাকলে হুবহু (verbatim) extract করো। না থাকলে, তথ্য থেকে নতুন MCQ বানাও।
- হাইলাইট/মার্ক/আন্ডারলাইন করা লাইন থাকলে সেগুলো থেকে অবশ্যই MCQ বানাবে (সবার আগে, মিস করা যাবে না)।
- বক্স/ছক/সারণিতে তথ্য থাকলে প্রতিটা থেকে অন্তত ১টা MCQ বানাও।
- Question ও option-এর তথ্য অবশ্যই এই page-এর নিজের content থেকে আসবে — বাইরের knowledge দিয়ে বানানো যাবে না।
- টপিকের নাম/হেডলাইন/পেইজ নম্বরের মতো navigation/label টেক্সট থেকে MCQ বানাবে না।
- একই প্রশ্ন দুইবার (হুবহু বা ঘুরিয়ে) বানানো যাবে না।
- ভাষা: source-এর ভাষায় লিখবে (বাংলা হলে বাংলা, ইংরেজি হলে ইংরেজি — translate করবে না)।

💥প্রশ্ন: ছোট (১-২ লাইন)
💥অপশন: ৪টি, সবগুলোই factual, একটাই সঠিক উত্তর (হ্যাঁ/না/সত্য/মিথ্যা না)
💥উত্তর: A/B/C/D — সব প্রশ্নে একই letter না, ছড়িয়ে দাও
-MUST বানাতে হবে exactly {count} টি MCQ, কম বেশি নয়
💥ব্যাখ্যা (MAX 165 শব্দ): সঠিক উত্তর কেন সঠিক + বাকি ৩টা কেন ভুল, শুধু page content থেকে (বাইরের knowledge না), source-reference phrase ("টেক্সট অনুসারে" ইত্যাদি) ছাড়া সরাসরি fact আকারে।

Topic: {topic}
Page: {page}

MUST Return ONLY valid JSON array, no markdown:
[{{"question":"...","options":["option1","option2","option3","option4"],"answer":"B","explanation":"..."}}]"""

MCQ_PROMPT_MAX = """📝 এই page-টা থেকে MCQ বানাও।

🎯 MAIN RULE — MAXIMUM CONTENT USE: পুরো page-এর প্রতিটা অংশ (প্রতিটা প্যারাগ্রাফ, লাইন, বক্স/ছক, হাইলাইট/মার্ক করা অংশ) ভালোভাবে পড়ো এবং page-এ যত তথ্য আছে তার maximum ব্যবহার করে MCQ বানাও — কোনো তথ্যবহুল অংশ বাদ দেওয়া যাবে না।

- Page-এ আগে থেকে MCQ (question+options) থাকলে হুবহু (verbatim) extract করো। না থাকলে, তথ্য থেকে নতুন MCQ বানাও।
- হাইলাইট/মার্ক/আন্ডারলাইন করা লাইন থাকলে সেগুলো থেকে অবশ্যই MCQ বানাবে (সবার আগে, মিস করা যাবে না)।
- বক্স/ছক/সারণিতে তথ্য থাকলে প্রতিটা থেকে অন্তত ১টা MCQ বানাও, তথ্য বেশি থাকলে একাধিক MCQ বানাও।
- Question ও option-এর তথ্য অবশ্যই এই page-এর নিজের content থেকে আসবে — বাইরের knowledge দিয়ে বানানো যাবে না।
- টপিকের নাম/হেডলাইন/পেইজ নম্বরের মতো navigation/label টেক্সট থেকে MCQ বানাবে না।
- একই প্রশ্ন দুইবার (হুবহু বা ঘুরিয়ে) বানানো যাবে না; হাবিজাবি/মানহীন MCQ বানানো যাবে না।
- ভাষা: source-এর ভাষায় লিখবে (বাংলা হলে বাংলা, ইংরেজি হলে ইংরেজি — translate করবে না)।

📊 COUNT: default target কমপক্ষে ১৫টি MCQ (user নির্দিষ্ট সংখ্যা না দিলে) — page-এ তথ্য বেশি থাকলে ৩৫ পর্যন্ত যেতে পারো, ৬-১০টায় থেমে যাওয়া চলবে না যতক্ষণ page-এ আরও extract-যোগ্য তথ্য আছে। তথ্য সত্যিই কম থাকলে minimum 10, একদম sparse হলে minimum 5।

💥প্রশ্ন: ছোট (১-২ লাইন), সব ধরনের angle থেকে (direct fact, reverse, cause-effect, comparison, "কোনটি সঠিক নয়" ইত্যাদি মিক্স)
💥অপশন: ৪টি, সবগুলোই factual, একটাই সঠিক উত্তর (হ্যাঁ/না/সত্য/মিথ্যা না)
💥উত্তর: A/B/C/D — সব প্রশ্নে একই letter না, ছড়িয়ে দাও
💥ব্যাখ্যা (MAX 165 শব্দ): সঠিক উত্তর কেন সঠিক + বাকি ৩টা কেন ভুল, শুধু page content থেকে (বাইরের knowledge না), source-reference phrase ("টেক্সট অনুসারে" ইত্যাদি) ছাড়া সরাসরি fact আকারে।

Topic: {topic}
Page: {page}

MUST Return ONLY valid JSON array, no markdown:
[{{"question":"...","options":["option1","option2","option3","option4"],"answer":"C","explanation":"..."}}]"""


# ============================================================
# /pdfs: SINGLE-CALL TOPIC-DETECT + SEGMENT-LOCKED MCQ GENERATION
# ============================================================
# Per user requirement: exactly ONE Gemini call per page must do everything —
# detect every main/sub topic (with OCR+content-based self-verify), lock each
# topic's content boundary, AND generate that topic's MCQs strictly from its
# own content, all in one response. No image cropping (would risk losing
# content at crop edges) — the model reads the FULL page image once and
# self-organizes its own MCQ output by topic, tagging each MCQ with which
# topic/sub-topic it came from. This makes leaking one topic's content into
# another topic's MCQ set an internal-consistency failure the model itself
# must avoid while writing the single JSON response, and the code-level
# post-filter below (_pdfs_reconcile_mcq_topics) double-checks the tags
# against the model's own detected topic list before anything is used.
PDFS_TOPIC_MCQ_PROMPT = """📝 Special MCQ TYPE: /pdfs Topic-wise Generation (SINGLE CALL — topic detection + MCQ generation together)

🎯 এই কাজটা ২টা ধাপে করবে, কিন্তু একটাই response-এ:

═══ ধাপ A: TOPIC DETECTION + SELF-VERIFY (MCQ বানানোর আগে বাধ্যতামূলক) ═══
পুরো page স্ক্যান করে সব MAIN TOPIC ও SUB TOPIC identify করো এই cue দিয়ে:
- MAIN TOPIC: content-এর উপরে/মাঝখানে (top-center), bold + অন্য টেক্সট থেকে বড় ফন্ট, প্রায়ই আলাদা background/box/boundary দিয়ে ঘেরা, আগে special marker/symbol থাকতে পারে।
- SUB TOPIC (optional): main topic-এর নিচে, ছোট ফন্ট, background হালকা ভিন্ন color হতে পারে (full-white না), আগে প্রায়ই colon (: বা ঃ), আগে marker/symbol থাকতে পারে।

প্রতিটা candidate-এর জন্য বাধ্যতামূলক VERIFY (সন্দেহ থাকুক বা না থাকুক সবসময়): heading OCR/visual read করে raw name বের করো, তারপর সেই heading-এর নিচের/আশেপাশের actual body content সম্পূর্ণ পড়ে বুঝে নাও content আসলে কী বিষয়ে। raw name আর content-এর প্রকৃত বিষয় না মিললে (বানান ভুল/OCR-misread/garbled), content বুঝে সঠিক নাম নিজে ঠিক করে দাও — content-ই আসল সত্য, heading-এর লেখা শুধু hint, blind copy কখনো না।

প্রতিটা confirmed topic-এর জন্য নির্ধারণ করো ঠিক কোন প্যারাগ্রাফ/লাইন/বক্স/সারণি তার নিজের content (content-boundary lock) — দুইটা topic-এর content কখনো overlap/split/duplicate করা যাবে না; overlap মনে হলে যে heading content-টার সবচেয়ে কাছে/উপরে সেটাই owner।

কোনো clear topic/sub-topic না পেলে single virtual topic ধরো: main="{topic}", sub=null (পুরো page-ই তার content)।

═══ ধাপ B: প্রতিটা confirmed topic/sub-topic-এর জন্য আলাদা করে MCQ বানাও ═══
🔒 SOURCE-LOCK + TOPIC-LOCK (ABSOLUTE): প্রতিটা MCQ শুধুমাত্র তার নিজের topic-এর ধাপ-A-তে lock করা content-boundary থেকেই বানাবে। একটা topic-এর MCQ-তে অন্য topic-এর content/fact কখনো মিশতে পারবে না, এমনকি অন্য topic-এ সহজ/ভালো content থাকলেও। প্রতিটা MCQ output-এ অবশ্যই সেটা কোন main_topic ও sub_topic থেকে এসেছে সেটা সঠিকভাবে লিখতে হবে (ধাপ A-তে ফাইনাল করা নাম অনুযায়ী, exact same spelling) — ভুল topic-এ MCQ tag করা কঠোরভাবে নিষিদ্ধ, কারণ এই ট্যাগ দিয়েই পরে output topic-wise ভাগ হবে।
🔴 STRICT PAGE-ONLY CONTENT (ABSOLUTE, প্রশ্ন+অপশন+ব্যাখ্যা সবক্ষেত্রে): question, প্রতিটা option, ও ব্যাখ্যা — সব শুধুমাত্র এই page-এ visible content থেকে আসবে। বাইরের সাধারণ জ্ঞান/training data থেকে কোনো fact, number, নাম, তারিখ, বা detail যোগ করা কঠোরভাবে নিষিদ্ধ — এমনকি সেটা সত্যি হলেও এবং topic-টা পরিচিত মনে হলেও। কোনো option-এর মধ্যে যদি page-এ না থাকা কোনো তথ্য বসাতে হয়, সেই MCQ-টাই বাদ দাও, বানিয়ে option দিও না। প্রতিটা MCQ লেখার আগে নিজেকে verify করো: "এই question/option-এর প্রতিটা শব্দ কি আমি এই page-এর ছবিতে সরাসরি দেখতে পাচ্ছি?" — না পারলে সেটা রাখা যাবে না।
-🔒 TWO-MODE RULE: page-এ আগে থেকেই MCQ (question+options) থাকলে সেগুলো 100% VERBATIM extract করবে, নইলে content থেকে নতুন MCQ বানাবে।
-🔒 NO-DUPLICATE-FROM-EXISTING-MCQ: existing question rephrase করে নতুন MCQ বানানো নিষিদ্ধ। NO-MCQ-FROM-ANSWER/EXPLANATION-TEXT: কোনো প্রশ্নের answer/explanation paragraph থেকে সরাসরি নতুন MCQ বানানো নিষিদ্ধ, শুধু actual info-content থেকেই বানাবে।
-🔴 HIGHLIGHT/MARK PRIORITY (ABSOLUTE FIRST): হাইলাইটেড/মার্ক করা/আন্ডারলাইন করা লাইন থেকে MCQ সবার আগে বানাবে (মিস করা যাবে না), তারপর বাকি normal content থেকে।
-প্রতিটা topic থেকে গড়ে {per_topic_count} টি MCQ target করো (topic-এ content বেশি/কম থাকলে স্বাভাবিকভাবে কমবেশি হতে পারে, কিন্তু কোনো topic 0 রাখা যাবে না যদি তার নিজের content থাকে)।
-টপিকের নাম/অধ্যায়ের নাম/হেডলাইন/পেইজ সংখ্যা/navigation label থেকে MCQ বানাবে না।
-প্রতিটা অপশন actual factual content হতে হবে, হ্যাঁ/না/সত্য/মিথ্যা না।
💥প্রশ্ন: ছোট (১/১.৫/২ লাইন)
💥অপশন: ৪টি, সঠিক উত্তর একটিই
💥উত্তর: A/B/C/D — বিভিন্ন position-এ ছড়িয়ে দিবে, সব একই letter না।
🔒 ANSWER RELEVANCY SANITY CHECK: page-এ answer আগে থেকে marked থাকলে, সেটা question+options-এর সাথে logically সঠিক কিনা re-check করো; স্পষ্ট mismatch হলেই শুধু নিজের জ্ঞান দিয়ে override করবে।
💥ব্যাখ্যা (MAX 165 WORDS মূল অংশ): সঠিক উত্তর কেন সঠিক + বাকি ৩টা কেন ভুল, সব মিলিয়ে ১৬৫ শব্দের মধ্যে; না আঁটলে extra detail নিচে আলাদা লাইনে, মূল অংশ কখনো truncate না। শুধু page content থেকে, বাইরের knowledge না। source-reference phrase ("টেক্সট অনুসারে" ইত্যাদি) নিষিদ্ধ।

🌐 LANGUAGE RULE: source-এর ভাষায় (বাংলা হলে বাংলা, ইংরেজি হলে ইংরেজি — translate করবে না)।

Page: {page}

MUST Return ONLY valid JSON array, no markdown, EVERY item MUST include main_topic + sub_topic:
[{{"main_topic":"...","sub_topic":"..." or null,"question":"...","options":["option1","option2","option3","option4"],"answer":"B","explanation":"..."}}]"""


PDFS_CALL2_MCQ_ONLY_PROMPT = """📝 /pdfs Call2 — MCQ GENERATION ONLY (topic detection already done in Call1, DO NOT re-detect)

🔒 CONFIRMED TOPICS FOR THIS PAGE (from Call1, already verified — use these EXACT names, do not rename/reinterpret):
{topics_list}

═══ প্রতিটা confirmed topic/sub-topic-এর জন্য আলাদা করে MCQ বানাও ═══
🔒 SOURCE-LOCK + TOPIC-LOCK (ABSOLUTE): প্রতিটা MCQ শুধুমাত্র তার নিজের topic-এর content-boundary থেকেই বানাবে (উপরের list-এর যে heading-এর নিচে/কাছে সেই content, সেটাই তার topic)। একটা topic-এর MCQ-তে অন্য topic-এর content/fact কখনো মিশতে পারবে না। প্রতিটা MCQ output-এ অবশ্যই সেটা কোন main_topic (উপরের list থেকে exact same spelling) থেকে এসেছে সেটা লিখতে হবে — যদি page-এ উপরের কোনো topic-এর সাথে না মেলে এমন content থাকে, সেটাকে সবচেয়ে কাছের/প্রাসঙ্গিক topic-এর আন্ডারে রাখো।
🔴 STRICT PAGE-ONLY CONTENT (ABSOLUTE, প্রশ্ন+অপশন+ব্যাখ্যা সবক্ষেত্রে): question, প্রতিটা option, ও ব্যাখ্যা — সব শুধুমাত্র এই page-এ visible content থেকে আসবে। বাইরের সাধারণ জ্ঞান/training data থেকে কোনো fact যোগ করা কঠোরভাবে নিষিদ্ধ। কোনো option-এর মধ্যে page-এ না থাকা তথ্য বসাতে হলে সেই MCQ-টাই বাদ দাও।
-🔒 TWO-MODE RULE: page-এ আগে থেকেই MCQ থাকলে 100% VERBATIM extract করবে, নইলে content থেকে নতুন MCQ বানাবে।
-🔒 NO-DUPLICATE-FROM-EXISTING-MCQ, NO-MCQ-FROM-ANSWER/EXPLANATION-TEXT.
-🔴 HIGHLIGHT/MARK PRIORITY (ABSOLUTE FIRST): হাইলাইটেড/মার্ক করা লাইন থেকে MCQ সবার আগে বানাবে।
-প্রতিটা topic থেকে গড়ে {per_topic_count} টি MCQ target করো, কোনো topic 0 রাখা যাবে না যদি তার নিজের content থাকে।
-টপিকের নাম/হেডলাইন/পেইজ সংখ্যা থেকে MCQ বানাবে না। প্রতিটা অপশন actual factual content, হ্যাঁ/না না।
💥প্রশ্ন: ছোট (১/১.৫/২ লাইন) | 💥অপশন: ৪টি, সঠিক উত্তর একটিই | 💥উত্তর: A/B/C/D ছড়িয়ে দিবে।
💥ব্যাখ্যা (MAX 165 WORDS): সঠিক উত্তর কেন সঠিক + বাকি ৩টা কেন ভুল, শুধু page content থেকে।

🌐 LANGUAGE RULE: source-এর ভাষায় (translate করবে না)।

Page: {page}

MUST Return ONLY valid JSON array, EVERY item MUST include main_topic + sub_topic:
[{{"main_topic":"...","sub_topic":"..." or null,"question":"...","options":["option1","option2","option3","option4"],"answer":"B","explanation":"..."}}]"""


def _pdfs_reconcile_mcq_topics(mcqs: list, fallback: str, allowed_topics: list = None) -> list:
    """Code-level backstop (not prompt-only) — runs on every /pdfs generation
    result before it's used anywhere else. Ensures every MCQ has a clean,
    non-empty main_topic (never silently dropped into the wrong bucket by a
    blank/garbled tag), normalizes sub_topic (blank/whitespace -> None), and
    strips the internal main_topic/sub_topic keys into the standard
    _pdfs_topic/_pdfs_subtopic keys app.py's grouping code expects — so a
    single point of truth decides the final topic bucket for every MCQ,
    instead of trusting the raw model output directly.

    allowed_topics (Call2 only): the exact main_topic names Call1 already
    confirmed for this page. If the model tags an MCQ with something NOT in
    this list (hallucinated/garbled topic name), force it onto the single
    allowed topic (if only one) or the page's fallback topic instead of
    creating a stray bucket that never appeared in Call1 -- this is what
    actually prevents one topic's MCQs from silently leaking into a wrong
    bucket."""
    out = []
    _allowed_norm = {t.strip().lower(): t.strip() for t in (allowed_topics or []) if t and t.strip()}
    for m in (mcqs or []):
        if not isinstance(m, dict) or not m.get("question"):
            continue
        main_t = (m.get("main_topic") or "").strip()
        if not main_t:
            main_t = fallback
        if _allowed_norm and main_t.strip().lower() not in _allowed_norm:
            main_t = next(iter(_allowed_norm.values())) if len(_allowed_norm) == 1 else fallback
        else:
            main_t = _allowed_norm.get(main_t.strip().lower(), main_t)
        sub_t = m.get("sub_topic")
        sub_t = sub_t.strip() if isinstance(sub_t, str) and sub_t.strip() else None
        m["_pdfs_topic"] = main_t[:60]
        m["_pdfs_subtopic"] = sub_t[:60] if sub_t else None
        m.pop("main_topic", None)
        m.pop("sub_topic", None)
        out.append(m)
    return out


async def generate_pdfs_topic_mcqs(img: Image.Image, topic: str, page: int, mcq_count_hint: int = 15) -> list:
    """/pdfs SINGLE-CALL pipeline: exactly ONE Gemini call per page does
    topic-detection + self-verification + content-boundary lock + MCQ
    generation together (see PDFS_TOPIC_MCQ_PROMPT above). Falls through the
    same Gemini key-rotation pool as the normal /pdf path — tries every live
    key before returning empty (caller's normal Groq/OpenRouter fallback
    chain then applies exactly as it does for /pdf). Returns a flat list of
    MCQ dicts, each already carrying _pdfs_topic/_pdfs_subtopic (see
    _pdfs_reconcile_mcq_topics) so app.py's per-page loop and end-of-job
    topic grouping can use them directly with zero extra topic-detect calls."""
    prompt = PDFS_TOPIC_MCQ_PROMPT.format(topic=topic, page=str(page).zfill(2), per_topic_count=mcq_count_hint)
    _ordered = key_rotator.ordered_keys(offset=_qbm_key_offset_ctx.get())
    _ordered = [k for k in _ordered if not _is_gemini_key_exhausted_today(k)] or _ordered
    if key_rotator.keys and all(_is_gemini_key_exhausted_today(k) for k in key_rotator.keys):
        logger.warning(f"[PDFS] All {len(key_rotator.keys)} Gemini keys daily-exhausted — returning empty (caller tries Groq/other fallbacks)")
        return []
    # User instruction (2026-08-25): try every live key before Groq -- only
    # stop early on a genuine backend/network outage (3 consecutive
    # non-quota failures), never just because a key-count ceiling was hit.
    max_retries = len(_ordered) if _ordered else 5
    _consecutive_infra_fails = 0
    for attempt in range(max_retries):
        key = _ordered[attempt % len(_ordered)] if _ordered else key_rotator.get_key()
        key_rotator.record_call(key)
        try:
            from google import genai as gai
            from google.genai import types
            client = gai.Client(api_key=key)
            img_b64 = image_to_base64(img)

            def _call():
                return client.models.generate_content(
                    model="gemini-3.6-flash",
                    contents=[
                        types.Part.from_text(text=prompt),
                        types.Part.from_bytes(data=base64.b64decode(img_b64), mime_type="image/jpeg")
                    ]
                )
            # 2026-08-22: 25-40s range across all keys (uncapped) -- gives
            # each key a fair chance to succeed while still failing fast
            # enough on genuinely dead/throttled keys.
            _attempt_timeout = 40 if attempt == 0 else 25
            response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=_attempt_timeout)
            valid = _parse_mcq_json(response.text)
            if not valid:
                logger.warning(f"[PDFS] Page {page}: 0 valid MCQs parsed (attempt {attempt+1}) — likely malformed/truncated JSON")
            else:
                valid = _pdfs_reconcile_mcq_topics(valid, topic)
            key_rotator.mark_healthy(key)
            logger.info(f"[PDFS] Page {page}: {len(valid)} MCQs across "
                        f"{len(set(m['_pdfs_topic'] for m in valid))} topic(s) (attempt {attempt+1})")
            return valid
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                is_daily = "PerDay" in err_str or "generate_content_free_tier_requests" in err_str
                key_rotator.mark_rate_limited(key, daily_exhausted=is_daily)
                _consecutive_infra_fails = 0
            elif "SUSPENDED" in err_str.upper() or "API_KEY_INVALID" in err_str.upper():
                key_rotator.mark_banned(key)
                _consecutive_infra_fails = 0
            else:
                logger.warning(f"[PDFS] Attempt {attempt+1} failed: {type(e).__name__}: {err_str}")
                _consecutive_infra_fails += 1
                if _consecutive_infra_fails >= 3:
                    logger.warning(f"[PDFS] {_consecutive_infra_fails} consecutive non-quota failures (page {page}) — backend appears down, stopping early.")
                    break
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
            continue
    logger.warning(f"[PDFS] All keys failed for page {page} — returning empty (caller will try Groq/other fallbacks)")
    return []


async def generate_pdfs_call2_mcqs(img: Image.Image, headings: list, topic: str, page: int,
                                    mcq_count_hint: int = 15, timing: dict = None) -> tuple:
    """/pdfs Call2 (generation-only): headings is Call1's already-confirmed
    list of [{"main":..., "sub":...}] for this page — this function does
    NOT re-detect topics, it just generates MCQs and tags each with one of
    the given topic names (see PDFS_CALL2_MCQ_ONLY_PROMPT). Splitting
    detection (Call1) and generation (Call2) into separate calls means
    topic-detect never depends on generation succeeding and vice versa —
    same two-call shape as /topic and /bio use elsewhere in this file.
    Returns (mcqs, elapsed_seconds, model_used) so the caller can show
    per-page timing + model in the live dashboard. If timing dict is
    passed, also records timing['start']/['end'] for external use."""
    import time as _time
    _t0 = _time.time()
    _headings_list = headings or [{"main": topic, "sub": None}]
    _allowed_topics = [h.get("main") for h in _headings_list if h.get("main")]
    topics_list = "\n".join(
        f"- main: \"{h.get('main')}\"" + (f", sub: \"{h.get('sub')}\"" if h.get('sub') else "")
        for h in _headings_list
    )
    prompt = PDFS_CALL2_MCQ_ONLY_PROMPT.format(
        topics_list=topics_list, page=str(page).zfill(2), per_topic_count=mcq_count_hint)
    _ordered = key_rotator.ordered_keys(offset=_qbm_key_offset_ctx.get())
    _ordered = [k for k in _ordered if not _is_gemini_key_exhausted_today(k)] or _ordered
    if key_rotator.keys and all(_is_gemini_key_exhausted_today(k) for k in key_rotator.keys):
        logger.warning(f"[PDFS-C2] All {len(key_rotator.keys)} Gemini keys daily-exhausted — returning empty")
        return [], round(_time.time() - _t0, 1), None
    max_retries = len(_ordered) if _ordered else 5
    _consecutive_infra_fails = 0
    for attempt in range(max_retries):
        key = _ordered[attempt % len(_ordered)] if _ordered else key_rotator.get_key()
        key_rotator.record_call(key)
        try:
            from google import genai as gai
            from google.genai import types
            client = gai.Client(api_key=key)
            img_b64 = image_to_base64(img)

            def _call():
                return client.models.generate_content(
                    model="gemini-3.6-flash",
                    contents=[
                        types.Part.from_text(text=prompt),
                        types.Part.from_bytes(data=base64.b64decode(img_b64), mime_type="image/jpeg")
                    ]
                )
            _attempt_timeout = 40 if attempt == 0 else 25
            response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=_attempt_timeout)
            valid = _parse_mcq_json(response.text)
            elapsed = round(_time.time() - _t0, 1)
            if not valid:
                logger.warning(f"[PDFS-C2] Page {page}: 0 valid MCQs parsed (attempt {attempt+1})")
            else:
                valid = _pdfs_reconcile_mcq_topics(valid, topic, allowed_topics=_allowed_topics)
            key_rotator.mark_healthy(key)
            try:
                import app as _app_mod
                _app_mod._bump_ai_call_count(_app_mod._current_job_chat_id_ctx.get(), model="Gemini")
            except Exception:
                pass
            logger.info(f"[PDFS-C2] Page {page}: {len(valid)} MCQs in {elapsed}s (attempt {attempt+1}, gemini-3.6-flash)")
            return valid, elapsed, "Gemini" if valid else None
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                is_daily = "PerDay" in err_str or "generate_content_free_tier_requests" in err_str
                key_rotator.mark_rate_limited(key, daily_exhausted=is_daily)
                _consecutive_infra_fails = 0
            elif "SUSPENDED" in err_str.upper() or "API_KEY_INVALID" in err_str.upper():
                key_rotator.mark_banned(key)
                _consecutive_infra_fails = 0
            else:
                logger.warning(f"[PDFS-C2] Attempt {attempt+1} failed: {type(e).__name__}: {err_str}")
                _consecutive_infra_fails += 1
                if _consecutive_infra_fails >= 3:
                    logger.warning(f"[PDFS-C2] {_consecutive_infra_fails} consecutive non-quota failures (page {page}) — backend appears down, stopping early.")
                    break
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
            continue
    elapsed = round(_time.time() - _t0, 1)
    logger.warning(f"[PDFS-C2] All keys failed for page {page} after {elapsed}s")
    return [], elapsed, None


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
        def _convert_batch(first: int, last: int):
            """Convert a whole page range in ONE convert_from_bytes call
            (single poppler subprocess) instead of one call per page --
            was the actual bottleneck causing long stalls after download
            hit 100% (19 pages = 19 separate subprocess spins). Falls back
            to per-page conversion only if the batch call itself fails."""
            try:
                imgs = convert_from_bytes(pdf_bytes, first_page=first, last_page=last, dpi=150, thread_count=4)
                if imgs and len(imgs) == (last - first + 1):
                    return imgs
            except Exception as _conv_e:
                logger.warning(f"[PDF] Batch convert pages {first}-{last} (dpi=150) raised: {_conv_e}")
            return None

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
            batch = _convert_batch(first, last)
            if batch is not None:
                result = list(zip(range(first, last + 1), batch))
                logger.info(f"[PDF] Converted {len(result)} pages (single batch call)")
                return result
            # Batch failed -- fall back to the slower but bulletproof
            # per-page path (with its own retry/dpi-fallback) so no page
            # is ever silently dropped.
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
            logger.info(f"[PDF] Converted {len(result)} pages (per-page fallback)")
            return result
        else:
            total_pages = get_pdf_page_count(pdf_bytes)
            if total_pages:
                cap = min(total_pages, _PDF_MAX_PAGES_PER_CALL)
                batch = _convert_batch(1, cap)
                if batch is not None:
                    result = list(zip(range(1, cap + 1), batch))
                    if total_pages > _PDF_MAX_PAGES_PER_CALL:
                        raise ValueError(f"PDF_TRUNCATED_AT:{_PDF_MAX_PAGES_PER_CALL}")
                    logger.info(f"[PDF] Converted {len(result)} pages (single batch call)")
                    return result
            # REAL BUG FIX: previously an empty convert_from_bytes result on
            # page N was treated as "end of document" via break — but empty
            # can also mean a TRANSIENT failure on a page that is NOT actually
            # the last page, silently truncating the rest of the PDF. Now we
            # retry+dpi-fallback per page, and only treat it as true
            # end-of-document once confirmed against the real page count.
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
            logger.info(f"[PDF] Converted {len(result)} pages (per-page fallback)")
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
    # Reasoning models sometimes prefix output with <think>...</think> —
    # strip it before markdown-fence handling / json.loads.
    if "<think>" in text:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        if not text or "<think>" in text:
            raise ValueError("Unclosed <think> block, no usable JSON")
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


def crop_option_image(img: Image.Image, bbox: list) -> str:
    """
    /qbm option-image support: a REAL tight crop of just the image sitting on
    an MCQ option — unlike crop_explanation_image() (which uploads the full
    page with a highlight overlay for CSS-cropped client rendering), this
    uploads ONLY the cropped region itself, since it's embedded as a normal
    <img> tag directly inside that option's CSV/DB text.
    Returns the uploaded url, or "" on failure.
    """
    if not bbox or len(bbox) != 4:
        return ""
    try:
        from atlas_mhtml import upload_to_imgbb
        w, h = img.size
        x_min, y_min, x_max, y_max = bbox
        # padding (2.5% of each dimension) so diagram edges/labels never get cut
        pad_x, pad_y = w * 0.025, h * 0.025
        left = max(0, int((x_min / 1000) * w - pad_x))
        top = max(0, int((y_min / 1000) * h - pad_y))
        right = min(w, int((x_max / 1000) * w + pad_x))
        bottom = min(h, int((y_max / 1000) * h + pad_y))
        if right <= left or bottom <= top:
            return ""
        crop = img.convert("RGB").crop((left, top, right, bottom))
        url = upload_to_imgbb(image_to_base64(crop))
        if not url:
            url = upload_to_imgbb(image_to_base64(crop))  # one retry
        return url or ""
    except Exception as e:
        logger.warning(f"[OptionImageCrop] Failed: {e}")
        return ""


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
        "google/gemma-4-31b-it:free,google/gemma-4-26b-a4b-it:free,nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
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
    _ordered = key_rotator.ordered_keys(offset=_qbm_key_offset_ctx.get())
    # Only attempt keys not already known-exhausted/banned today — retrying
    # a dead key wastes a full timeout slot for nothing, and skipping them
    # lets us cycle through ALL live keys within max_retries.
    _ordered = [k for k in _ordered if not _is_gemini_key_exhausted_today(k)] or _ordered

    # If every key is already known daily-exhausted (Pacific-day), skip Gemini
    # entirely instead of burning 429 round-trips we already know will fail —
    # go straight to OpenRouter fallback.
    if key_rotator.keys and all(_is_gemini_key_exhausted_today(k) for k in key_rotator.keys):
        logger.warning(f"[Gemini] All {len(key_rotator.keys)} keys already daily-exhausted for today — returning empty (caller will try Groq/other fallbacks)")
        return []

    # User instruction (2026-08-25): Gemini has many live keys -- Groq
    # should NEVER be touched while even one Gemini key is still alive
    # (not daily-exhausted). Try every live key before falling through.
    # Exception: if the Gemini BACKEND itself is down (not a per-key quota
    # issue -- e.g. network/503 outage), every key fails the same way, so
    # burning all of them (could be dozens) before falling back would stall
    # a single page for many minutes. Detect that case specifically: if the
    # first 3 consecutive attempts ALL fail with a timeout/connection/503
    # error (not quota/429/invalid-key), treat it as a backend outage and
    # stop early instead of exhausting the whole key list pointlessly.
    max_retries = len(_ordered) if _ordered else 5
    _consecutive_infra_fails = 0
    # Model fallback chain: try the latest model first, and if the WHOLE
    # Gemini backend for it is overloaded (503 UNAVAILABLE — this is a
    # server-side capacity issue, not a per-key problem, so it hits every
    # key the same way), drop to the older stable model on the same key
    # before moving to the next key. New models get more 503s in their
    # first weeks of traffic ramp-up.
    # 2026-08-07: switched back to gemini-3.6-flash — gemini-2.5-flash was
    # 404ing ("no longer available to new users") for new API keys, on top
    # of its own daily-quota exhaustion, so it's no longer a safe primary.
    _GEMINI_MODELS = ["gemini-3.6-flash"]

    for attempt in range(max_retries):
        key = _ordered[attempt % len(_ordered)] if _ordered else key_rotator.get_key()
        key_rotator.record_call(key)
        last_exc = None
        for model_name in _GEMINI_MODELS:
            try:
                from google import genai as gai
                from google.genai import types
                # FIX (2026-08-23): the client had no HTTP-level timeout, so
                # asyncio.wait_for() only stopped *waiting* on the outer
                # coroutine -- the underlying request in the background
                # thread kept running for the SDK's own (much larger)
                # default, meaning every attempt silently burned its full
                # 40s/25s budget instead of failing fast. Setting an
                # explicit http_options timeout (slightly under our
                # asyncio timeout, in ms) lets the real network call give
                # up on its own so the retry loop can move to the next key
                # promptly instead of stalling on a dead/slow connection.
                _http_timeout_ms = 35000 if attempt == 0 else 20000
                client = gai.Client(
                    api_key=key,
                    http_options=types.HttpOptions(timeout=_http_timeout_ms)
                )
                img_b64 = image_to_base64(img)

                def _call_gemini():
                    return client.models.generate_content(
                        model=model_name,
                        contents=[
                            types.Part.from_text(text=prompt),
                            types.Part.from_bytes(
                                data=base64.b64decode(img_b64),
                                mime_type="image/jpeg"
                            )
                        ]
                    )

                # 2026-08-22: 25-40s range across all keys (uncapped) --
                # gives each key a fair chance to succeed while still
                # failing fast enough on genuinely dead/throttled keys.
                _attempt_timeout = 40 if attempt == 0 else 25
                response = await asyncio.wait_for(asyncio.to_thread(_call_gemini), timeout=_attempt_timeout)
                valid = _parse_mcq_json(response.text)
                if not valid:
                    try:
                        from app import record_empty_parse
                        record_empty_parse("gemini")
                    except Exception:
                        pass
                    logger.warning(f"[Gemini] Page {page}: response OK but 0 valid MCQs parsed (attempt {attempt+1}, model={model_name}) — likely malformed/truncated JSON, not a real 'page has no content'")
                key_rotator.mark_healthy(key)
                logger.info(f"[Gemini] Page {page}: {len(valid)} MCQs (attempt {attempt+1}, model={model_name})")
                return valid
            except Exception as e:
                last_exc = e
                err_str = str(e)
                if "503" in err_str or "UNAVAILABLE" in err_str.upper():
                    try:
                        from app import classify_ai_error
                        classify_ai_error(e, "gemini")
                    except Exception:
                        pass
                    logger.warning(f"[Gemini] {model_name} overloaded (503) on attempt {attempt+1} — trying next model on same key")
                    continue
                # Not a 503 — no point trying the fallback model on this key,
                # move straight to key-level handling below.
                break
        e = last_exc
        if e is None:
            continue  # shouldn't happen, but guard just in case
        err_str = str(e)
        err_label = f"{type(e).__name__}: {err_str}" if err_str else f"{type(e).__name__} (no message — likely timeout)"
        try:
            from app import classify_ai_error
            classify_ai_error(e, "gemini")
        except Exception:
            pass  # classifier is best-effort visibility only, never blocks the real retry logic
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
            is_daily = "PerDay" in err_str or "generate_content_free_tier_requests" in err_str
            if is_daily:
                logger.warning(f"[Gemini] Attempt {attempt+1}: daily quota exhausted — skipping this key until Pacific-day quota reset: {err_label}")
            else:
                logger.warning(f"[Gemini] Attempt {attempt+1} rate-limited (429), cooling down key for {key_rotator.COOLDOWN_SECONDS}s: {err_label}")
            key_rotator.mark_rate_limited(key, daily_exhausted=is_daily)
            _consecutive_infra_fails = 0  # quota issue is per-key, not backend-wide -- keep trying other live keys
        elif "SUSPENDED" in err_str.upper() or "API_KEY_INVALID" in err_str.upper():
            logger.error(f"[Gemini] Attempt {attempt+1}: key permanently banned (suspended/invalid): {err_label}")
            key_rotator.mark_banned(key)
            _consecutive_infra_fails = 0  # per-key issue, not backend-wide
        else:
            # Timeout / connection error / non-429-503 exception -- this is
            # the "backend/network genuinely down" signature, not a per-key
            # problem, since every key would hit the same failure.
            logger.warning(f"[Gemini] Attempt {attempt+1} failed (both models): {err_label}")
            _consecutive_infra_fails += 1
            if _consecutive_infra_fails >= 3:
                logger.warning(f"[Gemini] {_consecutive_infra_fails} consecutive non-quota failures (page {page}) — Gemini backend/network appears down, stopping early instead of burning all {max_retries} keys. Falling to Groq/other fallbacks.")
                break
        if attempt < max_retries - 1:
            await asyncio.sleep(1)
        continue

    logger.warning(f"[Gemini] All keys failed for page {page} — returning empty (caller will try Groq/other fallbacks)")

    # NOTE: OpenRouter fallback removed from here (2026-08) — this function
    # is called as the PRIMARY step from app.py's _generate_mcq_from_image_raw,
    # which itself falls to Groq next, then to _AI_PROVIDERS_ORDER (nvidia/
    # openrouter_qwen/nemotron/gemma/hf). Calling OpenRouter here too meant
    # Groq was being skipped entirely whenever Gemini failed, since this
    # function would already return a (possibly empty) OpenRouter result
    # before app.py ever got a chance to try Groq.
    return []



async def generate_mcq_from_text(text: str, topic: str = "MCQ", count: int = 15) -> list:
    # 2026-08-22 BUGFIX: previously wrapped in MCQ_PROCESSING_QUEUE_LOCK, a
    # single global asyncio.Lock -- meant only ONE /txt job could run across
    # THE WHOLE BOT at once, so User B's /txt request sat fully blocked
    # behind User A's until A's finished, even though each call already uses
    # its own independent Gemini/Groq API key + connection and has nothing
    # that actually needs to be serialized. Removed: each call now runs
    # concurrently, so multiple users' /txt jobs never queue behind each other.
    return await _generate_mcq_from_text_raw(text, topic, count)


async def _generate_mcq_from_text_raw(text: str, topic: str = "MCQ", count: int = 15) -> list:
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
    _ordered = key_rotator.ordered_keys(offset=_qbm_key_offset_ctx.get())
    for attempt in range(max_retries):
        try:
            key = _ordered[attempt % len(_ordered)] if _ordered else key_rotator.get_key()
            key_rotator.record_call(key)
            from google import genai as gai
            from google.genai import types
            # FIX (same class of bug as the image-path _call_gemini): no
            # client-level HTTP timeout meant asyncio.wait_for(timeout=45)
            # only stopped the outer await -- the background thread's real
            # request kept running on the SDK's own default, so a slow/dead
            # connection burned the full 45s every attempt instead of
            # failing fast into the next key.
            client = gai.Client(
                api_key=key,
                http_options=types.HttpOptions(timeout=38000)
            )

            def _call_gemini():
                return client.models.generate_content(
                    model="gemini-3.6-flash",
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
                is_daily = "PerDay" in err_str or "generate_content_free_tier_requests" in err_str
                key_rotator.mark_rate_limited(key, daily_exhausted=is_daily)
                logger.warning(f"[Gemini-Text] Attempt {attempt+1} rate-limited (429){' [daily quota]' if is_daily else ''}, cooling down: {e}")
            elif "SUSPENDED" in err_str.upper() or "API_KEY_INVALID" in err_str.upper():
                logger.error(f"[Gemini-Text] Attempt {attempt+1}: key permanently banned (suspended/invalid): {e}")
                key_rotator.mark_banned(key)
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

