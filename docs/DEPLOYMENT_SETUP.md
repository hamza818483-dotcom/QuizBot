# QuizBot Deployment Setup & Failover Guide

_Last updated: 2026-07-08 — after HF + Cloudflare Pages migration_

## Current Live Architecture

```
Telegram → CF Pages (quizbot.pages.dev) /webhook → forwards to → HF Space (hamza-02-quizbot.hf.space)
```

- **Bot code runs on:** Hugging Face Space `hamza-02/QuizBot` (primary, currently active)
- **Proxy layer:** Cloudflare Pages project `quizbot` (NOT Cloudflare Workers — see "Why Pages not Workers" below)
- **Fallback option:** Render service (suspended, kept ready for manual failover)
- **Database:** Supabase (primary `wbdyjpjbczfunyhhmtry.supabase.co`, secondary `SB2_URL/SB2_KEY` hardcoded fallback in code)
- **D1:** Cloudflare D1 `atlasbot-db` (bound to Pages project as `DB`)

---

## Why Cloudflare Pages, not Workers

Hugging Face Spaces **blocks outbound network calls to `*.workers.dev`** domains but allows `*.pages.dev`.

Symptom seen: bot logs showed `ConnectError` / `ConnectTimeout` on every call to
`atlasquizbotpro.hamza818483.workers.dev`, both via proxy and via direct
`api.telegram.org` fallback — meaning HF's network sandbox was silently
dropping/timing out connections to the `workers.dev` domain.

**Fix:** Same `worker.js` code redeployed as a Cloudflare **Pages** project
using the `_worker.js` "advanced mode" — Pages projects get a `*.pages.dev`
domain, which HF can reach fine. No code logic changed, only the hosting
platform + one hardcoded URL reference inside the file (`WORKER_ORIGIN`).

**Pages project setup gotcha:** Build output directory MUST be set to the
folder containing `_worker.js` (here: `pages_deploy`). If left blank,
Cloudflare defaults to serving from repo root, `_worker.js` never gets
picked up, and all proxy routes 405/404 silently.

---

## Environment Variables Reference

### HF Space secrets (Settings → Repository secrets)

Required (bot won't start / won't work correctly without these):
```
BOT_TOKEN
API_ID
API_HASH
SESSION_STRING
SUPABASE_URL
SUPABASE_KEY
D1_TOKEN
GEMINI_KEYS
GROQ_API_KEY          ⚠️ must be named exactly this — NOT "GROQ_KEYS"
OPENROUTER_KEYS
IMGBB_API_KEYS
CF_WORKER_URL         → https://quizbot.pages.dev
HF_SPACE_URL          → https://hamza-02-quizbot.hf.space
ATLASBOT_URL
OWNER_ID
RENDER_URL            → currently https://hamza-02-quizbot.hf.space (see failover section)
RUNNING_ON            → HuggingFace Space
```

NOT needed — already hardcoded with safe defaults in code (core.py):
```
CHROMIUM_PATH     → defaults to "chromium"
LOG_DIR           → defaults to "logs"
GH_PAGES_EXAM_URL → defaults to GitHub Pages URL
SB2_URL / SB2_KEY → hardcoded fallback Supabase creds already in core.py
GEMMA_API_KEY / HF_API_KEY / HF_VISION_MODEL / NEMOTRON_API_KEY / NVIDIA_API_KEY
                  → optional AI-provider fallback keys, empty = provider skipped
```

Not used anywhere in code (safe to ignore): `WEBHOOK_URL`, `OPENROUTER_MODELS`

### Cloudflare Pages project variables (Settings → Environment variables)
```
ATLAS_BOT_TOKEN   (same value as BOT_TOKEN)
RENDER_URL        → points to whichever backend is currently primary
                    (HF: https://hamza-02-quizbot.hf.space)
                    (Render: https://quizbot-zo6x.onrender.com)
                    ⚠️ ALWAYS include https:// — missing protocol breaks fetch() silently
RENDER_URL_2      → leave empty (no secondary needed currently)
OWNER_ID
D1_TOKEN
```
Plus **D1 database binding**: binding name `DB` → database `atlasbot-db`.

### Cloudflare Pages build settings (Settings → Builds & deployments)
```
Build command:        (leave blank)
Build output directory: pages_deploy
Root directory:        (leave blank / repo root)
Production branch:     main
```

---

## How Routing/Failover Actually Works

1. **Telegram webhook** is set to the CF Pages URL: `https://quizbot.pages.dev/webhook`
   (This basically never needs to change — it's the stable front door.)

2. **CF Pages `_worker.js`** receives the webhook, ACKs Telegram instantly, then
   forwards the update in the background to whatever `env.RENDER_URL` points to
   (function `forwardToHF()` in worker.js — name is historical, it just forwards
   to the URL in `RENDER_URL`, regardless of whether that's actually HF or Render).

3. **To switch primary backend between HF and Render:**
   - Just change the `RENDER_URL` variable in the CF Pages dashboard
   - No code change needed, no webhook change needed
   - Remember the `https://` prefix

4. **HF startup webhook self-check** (app.py, in `_supervised` startup block):
   - On every HF Space restart, the bot checks Telegram's current webhook URL
     via `tg_post("getWebhookInfo", {})` (uses CF proxy, NEVER calls
     `api.telegram.org` directly — that's blocked from HF's network)
   - If it doesn't match `CF_WORKER_URL + "/webhook"`, it auto-corrects it
   - This means webhook drift (e.g. someone manually pointing it elsewhere)
     self-heals on next HF restart

---

## Failover Runbook: HF Down → Switch to Render

1. Render dashboard → **Resume/redeploy** the suspended service
2. Wait for Render's own `/health` endpoint to return 200
3. CF Pages dashboard → Settings → Environment variables → `RENDER_URL`
   → change to `https://quizbot-zo6x.onrender.com` (with https://)
4. That's it — next webhook hit routes to Render automatically
5. No Telegram webhook change needed, no HF change needed

## Failover Runbook: Render Down → Switch back to HF

1. CF Pages dashboard → `RENDER_URL` → change to `https://hamza-02-quizbot.hf.space`
2. Done — same reasoning as above

---

## Known Non-Critical Errors (safe to ignore)

These appear in HF logs but do NOT affect bot functionality:
```
[TG] setMyCommands proxy error: ...
[TG] setChatMenuButton proxy error/direct error: ...
[SetCommand] failed for admin ...: None
```
These come from Telegram's per-admin custom command menu setup, which
intermittently fails (rate limits / transient network blips). The bot's
core message handling is unaffected.

---

## Debugging Checklist (if bot stops responding again)

1. Check Telegram webhook status:
   ```
   https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo
   ```
   Look at `url`, `last_error_message`, `pending_update_count`.

2. If `url` is NOT `https://quizbot.pages.dev/webhook` → something reset it.
   Re-set manually:
   ```
   https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://quizbot.pages.dev/webhook
   ```

3. Check HF Space `/health`:
   ```
   https://hamza-02-quizbot.hf.space/health
   ```
   Should return `{"status":"ok","db":true,...}`

4. Check HF Space logs for `POST /webhook HTTP/1.1" 200 OK` lines — if these
   ARE appearing but no reply reaches Telegram, the problem is in
   `tg_post()` → check for `ConnectError`/`ConnectTimeout` (network block)
   vs actual API errors (check `result.get('description')`).

5. Check CF Pages `RENDER_URL` variable — must have `https://` prefix and
   point to a currently-alive backend.

6. If CF Pages routes return 405 on `/tg-proxy/*` paths → Build output
   directory setting is wrong / `_worker.js` isn't being picked up. Check
   Settings → Builds & deployments → Build output directory = `pages_deploy`.
