"""
Gemini model vs prompt vs environment ভাবে diagnose করার জন্য standalone script।
এটা bot-এর ভেতর চালানো লাগবে না — সরাসরি command line থেকে চালাও:

    GEMINI_KEYS="key1,key2" python3 diagnose_gemini.py

কী টেস্ট করে:
1. gemini-flash-latest vs gemini-flash-latest — দুই model দিয়ে একই সহজ prompt,
   সময় compare করে (model-level issue হলে একটাতেই স্লো/fail হবে)
2. ছোট prompt (~200 token) vs বড় prompt (আসল bot prompt, ~3000+ token) —
   একই model দিয়ে, সময় compare করে (prompt-length issue হলে বড় prompt-এ
   অনেক বেশি সময় লাগবে)
3. Text-only vs Image+text — image processing নিজেই কতটা সময় নেয় সেটা আলাদা করে

Output থেকে বোঝা যাবে:
- যদি gemini-flash-latest consistently দ্রুত আর gemini-flash-latest consistently
  স্লো/timeout হয় → এটা MODEL-level issue (3.6 এখনো নতুন/busy)
- যদি ছোট prompt দ্রুত কিন্তু বড় prompt-এ বেশি সময় লাগে (উভয় model-এই)
  → এটা PROMPT-length/output-size issue, model-এর দোষ না
- যদি সব combination-ই ধারাবাহিকভাবে স্লো (>20s) হয় → এটা network/
  environment (HF Space) issue হতে পারে, Gemini নিজে না
"""
import os
import sys
import time
import asyncio
import base64
from io import BytesIO

try:
    from google import genai as gai
    from google.genai import types
except ImportError:
    print("❌ google-genai package নাই। `pip install google-genai --break-system-packages` দিয়ে install করো।")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("❌ Pillow package নাই। `pip install Pillow --break-system-packages` দিয়ে install করো।")
    sys.exit(1)


def get_key():
    raw = os.environ.get("GEMINI_KEYS", "") or os.environ.get("GEMINI_API_KEY", "")
    if not raw:
        print("❌ GEMINI_KEYS বা GEMINI_API_KEY env var সেট নাই।")
        print('   ব্যবহার: GEMINI_KEYS="your_key_here" python3 diagnose_gemini.py')
        sys.exit(1)
    return raw.split(",")[0].strip()


def make_test_image(w=1000, h=1400):
    """একটা সাদা টেস্ট ইমেজ বানায় (dummy — আসল book page না, শুধু timing test-এর জন্য)"""
    img = Image.new("RGB", (w, h), color=(255, 255, 255))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


SHORT_PROMPT = "Say hello in one short sentence."

LONG_PROMPT = """You are an expert MCQ-extraction engine for Bengali/English academic
textbook pages (medical/HSC/admission-standard quality).
Topic: Test Topic

TARGET 10-20 MCQs (no fixed number given by user, default target):
Extract quality MCQs covering the important information on this page —
typically 10-20 for a normal page. Fewer (5-10) is fine if the page
genuinely has little content; more (up to 25) only if the page is
unusually content-rich. Do not force-pad with repetitive re-angled
versions of the same fact just to hit a higher number — quality and
genuine coverage matter more than quantity.

Since this is a blank test image, just return an empty JSON array: []
Return STRICT JSON array only, no prose, no markdown fences.
""" + ("Additional filler context for length testing. " * 100)


async def run_test(client, model, prompt, img_b64=None, label=""):
    t0 = time.time()
    try:
        contents = [types.Part.from_text(text=prompt)]
        if img_b64:
            contents.append(types.Part.from_bytes(data=base64.b64decode(img_b64), mime_type="image/jpeg"))

        def _call():
            return client.models.generate_content(model=model, contents=contents)

        resp = await asyncio.wait_for(asyncio.to_thread(_call), timeout=90)
        elapsed = time.time() - t0
        out_len = len(resp.text) if resp.text else 0
        print(f"✅ {label:45s} | {elapsed:6.2f}s | output {out_len} chars")
        return elapsed, True
    except asyncio.TimeoutError:
        elapsed = time.time() - t0
        print(f"⏱️  {label:45s} | {elapsed:6.2f}s | TIMEOUT (90s)")
        return elapsed, False
    except Exception as e:
        elapsed = time.time() - t0
        print(f"❌ {label:45s} | {elapsed:6.2f}s | ERROR: {type(e).__name__}: {str(e)[:120]}")
        return elapsed, False


async def main():
    key = get_key()
    client = gai.Client(api_key=key)
    img_b64 = make_test_image()

    print("=" * 90)
    print("GEMINI MODEL/PROMPT DIAGNOSTIC — এটা bot না, standalone timing test")
    print("=" * 90)

    tests = [
        ("gemini-flash-latest", SHORT_PROMPT, None, "2.5-flash + short prompt (no image)"),
        ("gemini-flash-latest", SHORT_PROMPT, None, "3.6-flash + short prompt (no image)"),
        ("gemini-flash-latest", SHORT_PROMPT, img_b64, "2.5-flash + short prompt + image"),
        ("gemini-flash-latest", SHORT_PROMPT, img_b64, "3.6-flash + short prompt + image"),
        ("gemini-flash-latest", LONG_PROMPT, img_b64, "2.5-flash + LONG bot-style prompt + image"),
        ("gemini-flash-latest", LONG_PROMPT, img_b64, "3.6-flash + LONG bot-style prompt + image"),
    ]

    results = []
    for model, prompt, img, label in tests:
        elapsed, ok = await run_test(client, model, prompt, img, label)
        results.append((label, elapsed, ok))
        await asyncio.sleep(2)  # avoid back-to-back rate limiting skewing results

    print("\n" + "=" * 90)
    print("সারাংশ / ANALYSIS")
    print("=" * 90)

    fast_25 = [e for l, e, ok in results if "2.5-flash" in l and ok]
    fast_36 = [e for l, e, ok in results if "3.6-flash" in l and ok]
    short_p = [e for l, e, ok in results if "short prompt" in l and ok]
    long_p = [e for l, e, ok in results if "LONG" in l and ok]

    if fast_25 and fast_36:
        avg25, avg36 = sum(fast_25) / len(fast_25), sum(fast_36) / len(fast_36)
        print(f"গড় সময় — gemini-flash-latest: {avg25:.2f}s | gemini-flash-latest: {avg36:.2f}s")
        if avg36 > avg25 * 1.5:
            print("👉 3.6-flash উল্লেখযোগ্যভাবে ধীর — এটা MODEL-level issue (নতুন model, বেশি busy)")
        else:
            print("👉 দুই model-এর গতি কাছাকাছি — model-level সমস্যা না")

    if short_p and long_p:
        avg_s, avg_l = sum(short_p) / len(short_p), sum(long_p) / len(long_p)
        print(f"গড় সময় — short prompt: {avg_s:.2f}s | long (bot-style) prompt: {avg_l:.2f}s")
        if avg_l > avg_s * 2:
            print("👉 বড় prompt/output-এ অনেক বেশি সময় লাগছে — এটা PROMPT/OUTPUT-SIZE issue, model-এর দোষ না")
        else:
            print("👉 prompt size সময়ের উপর তেমন প্রভাব ফেলছে না")

    print("\nযদি সব combination-ই 20s+ সময় নেয়, বা timeout হয় — তাহলে network/HF Space")
    print("environment-এর সাথে সম্পর্কিত হতে পারে (Gemini নিজে থেকে না)।")


if __name__ == "__main__":
    asyncio.run(main())
