# Known Issues & Fixes Log (QuizBot)

এই ফাইলে যেসব bug fix হয়েছে সেগুলো লেখা থাকে, যাতে ভবিষ্যতে একই error দেখলে দ্রুত চেনা যায় এবং re-fix করতে না হয়।

---

## 1. Groq `413 Payload Too Large` on ALL keys (qwen/qwen3.6-27b)

**Log pattern:**
```
[AI-ROT] qwen/qwen3.6-27b HTTP 413: "Request too large for model qwen/qwen3.6-27b ... 
tokens per minute (TPM): Limit 8000, Requested 8501/11443 ..."
[Groq] key #N/12 failed (HTTP 413), trying next key
```

**Root cause:** Groq's `qwen/qwen3.6-27b` (the current vision-capable replacement for the
deprecated `llama-4-scout`) has an **8000 TPM hard limit**. A full-resolution page image
(base64 + vision tokenization) is consistently 8500-11500+ tokens — it **always** exceeds
the limit on **every single key**, wasting ~11s per generation before falling to Gemini.

**Fix (already applied):** `_img_to_data_url_groq()` in `app.py` — downscales image to
max 1024px dimension + JPEG quality 70, specifically for Groq calls only (Gemini/OpenRouter
use full-resolution `_img_to_data_url()` since their limits are much higher).

**Call sites that MUST use `_img_to_data_url_groq()` (not `_img_to_data_url()`):**
- `_gen_groq()` (main image MCQ generation)
- `_gen_groq_raw_text()` (`[GroqVerify]`)
- The relaxed-fallback Groq call (~line 7420s, loosened prompt pass)
- `_qbm_groq_call()` (`[Groq-QBM2]`, answer-key detection, box-extraction)
- The answer-key-detection Groq call (~line 9690s)

**If this error reappears:** search app.py for `_img_to_data_url(img)` (WITHOUT `_groq`
suffix) near any `qwen/qwen3.6-27b` call — someone reverted or added a new call site
without the resize.

---

## 2. OpenRouter `404 Not Found` on ALL attempts (qwen2.5-vl models)

**Log pattern:**
```
[OpenRouter] Attempt 1, model: qwen/qwen2.5-vl-72b-instruct:free
HTTP Request: POST https://openrouter.ai/api/v1/chat/completions "HTTP/1.1 404 Not Found"
[OpenRouter] HTTP 404 on attempt 1
... (repeats for all attempts/models) ...
[OpenRouter] All attempts failed for page 1
```

**Root cause:** `qwen/qwen2.5-vl-72b-instruct:free`, `qwen/qwen2.5-vl-32b-instruct:free`,
and `qwen/qwen-2-vl-72b-instruct` were **retired from OpenRouter** (confirmed via
`https://openrouter.ai/api/v1/models` — not in the current catalog at all as of July 2026).
Every single OpenRouter fallback attempt 404'd, meaning OpenRouter was a dead fallback
layer for months without anyone noticing (it only shows up when Groq+Gemini both fail too).

**Fix (already applied):** Replaced with confirmed-live free vision models:
- `google/gemma-4-31b-it:free` (primary — text+image, 262K context)
- `google/gemma-4-26b-a4b-it:free` (secondary)
- `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` (tertiary)

**Locations fixed:** `OPENROUTER_MODELS` in `pdf_handler.py`, `_gen_openrouter_qwen()`
and `_gen_gemma()` in `app.py`.

**If this error reappears:** OpenRouter free-model IDs churn frequently (models get
retired/renamed every few months). Fetch `https://openrouter.ai/api/v1/models`, filter
for `pricing.prompt == "0"` AND `architecture.input_modalities` containing `"image"`,
and swap in whatever is currently live. Do NOT assume last year's model name still exists.

---

## 3. `message is not modified` triggering full CF-fallback cascade

**Log pattern:**
```
[TG] editMessageText proxy failed: Bad Request: message is not modified: specified new 
message content and reply markup are exactly the same as a current content and reply 
markup of the message
[TG] editMessageText secondary CF proxy also failed: Bad Request: message is not modified...
[TG] editMessageText both CF endpoints failed — waiting 2.5s and retrying once more
[TG] editMessageText CF proxy failed and direct API is blocked on this platform — giving up
```

**Root cause:** This is NOT a real failure — it happens when a rapid progress-bar/spinner
edit lands on identical text to the previous edit (common with fast-ticking loading
animations). `tg_post()` in `core.py` was treating ANY non-`ok` Telegram response as a
failure, triggering secondary-endpoint fallback + a 2.5s retry-wait + eventually "giving
up" — all for something that isn't actually broken (the message already shows the right
content).

**Fix (already applied):** Both `_try_primary()` and `_try_secondary()` inside `tg_post()`
now check for `"message is not modified"` in the error description and treat it as a
success (`return result, True`) instead of a failure.

**If this error reappears:** Check that the `"message is not modified" in desc.lower()`
check is still present in both `_try_primary` and `_try_secondary` in `core.py`'s
`tg_post()` function — someone may have reverted it during a merge.

---

## 4. Secondary Supabase account (`xnkuuzstschdovcyomfk.supabase.co`) DNS failure

**Log pattern:**
```
[D1] Supabase mirror failed (https://xnkuuzstschdovcyomfk.supabase.co): 
[Errno -2] Name or service not known
```
(repeats on every single D1 write)

**Root cause:** DNS resolution failing for this specific hostname on every call — most
likely this Supabase project was paused or deleted (common on free tier after inactivity).
This is an **account/infra issue, not a code bug** — needs manual check on
https://supabase.com/dashboard to see if the `xnkuuzstschdovcyomfk` project is still active.

**Fix (already applied, code-side only):** Suppressed repeated identical warnings —
logs once via `_SB2_DOWN` flag in `core.py`, stays quiet until it recovers, instead of
spamming a warning on every single write.

**Action still needed (not code):** Check Supabase dashboard — is this project
paused/deleted? If intentionally retired, consider removing `SB2_URL`/`SB2_KEY` from
`core.py` entirely instead of leaving it silently failing forever.

---

## 5. Gemini `429 RESOURCE_EXHAUSTED` / `403 CONSUMER_SUSPENDED`

**Log pattern:**
```
429: "Quota exceeded for metric: generate_content_free_tier_requests, limit: 20"
403: "Permission denied: Consumer 'api_key:AIzaSyD7...' has been suspended"
```

**Root cause:** Free-tier Gemini quota (20 requests/day per key) genuinely exhausted, and
at least one key (`AIzaSyD7twoiLiDSB3L2fPcR_Ch0mx_rWBFSpf4`) has been suspended by Google
entirely (not just rate-limited — this key will never work again until manually
reactivated in Google AI Studio, if possible).

**This is NOT purely a code bug** — 429 is expected/handled gracefully (60s cooldown,
tries next key). But a **suspended key** should ideally be identified and removed/replaced
since it will keep failing every rotation cycle forever with no recovery.

**Action needed (not code):** Check Google AI Studio for the suspended key, replace it in
`GEMINI_KEYS` env var if a working replacement is available.

---

## General debugging tip
When "[AI-ROT] all providers exhausted for page 1" appears, check the error immediately
above it for EACH provider in order (Groq → Gemini → OpenRouter → NVIDIA/Nemotron/Gemma/HF)
— usually only one is a real infra problem (e.g. Gemini quota) and the rest are code bugs
(wrong model name, wrong image size) that are actually fixable, unlike the infra one.
