# Rules for any agent/dev working on this repo

## Current host: HuggingFace Space (16GB RAM) — Render is NOT in active use

Render (512MB RAM) was the original host and is why a lot of code has
RAM-saving tuning. The bot now runs on HF Space, which has ample RAM.
**Do not assume RAM is tight. Do not add new RAM-saving limits/caps
"just in case."** If Render is ever brought back into rotation, re-check
this file and `KNOWN_ISSUES.md` section 6 first.

## MANDATORY: any host-specific tuning MUST be gated by `IS_RENDER`

`IS_RENDER` (defined in `core.py`, imported into `app.py`/`pdf_handler.py`)
is the single source of truth for "are we on the tiny Render box or the
roomy HF Space." Every constant that trades functionality/capacity for RAM
— cache sizes, concurrency semaphores, Chromium launch flags, page/pixel
limits, byte caps, etc. — **must** be written as:

```python
SOME_LIMIT = tight_value if IS_RENDER else roomy_value
```

**Never** hardcode a Render-sized limit unconditionally, even "temporarily."
Two real bugs happened from exactly this mistake (2026-11-14):
- `--js-flags=--max-old-space-size=128` on the Playwright Chromium launch was
  added unconditionally and crashed the browser on launch on HF (no RAM
  issue at all — the flag itself broke things). Fixed in commit `08ef75b`.
- `_SHEET_CACHE_MAX` / `_SHEET_CACHE_MAX_BYTES` (`/sheet` command) were
  hardcoded to Render-sized values (8 entries / 6MB) with no `IS_RENDER`
  check, so HF users got a "CSV too big" rejection with no RAM problem
  behind it. Fixed in commit `5612c6d`.

Before adding or changing ANY limit/flag/cap related to memory, CPU
concurrency, or Chromium/Playwright launch args:
1. Check whether it's actually needed on the current host (HF, 16GB) —
   usually it is not.
2. If it only matters on Render, gate it with `if IS_RENDER else`.
3. Never ship a Render-only safety net as the unconditional default.

## Before pushing any fix: sanity-check the full file, not just the diff

- Run `python3 -c "import ast; ast.parse(open('app.py').read())"` (or the
  relevant file) after every edit — a broken edit must never reach `git push`.
- When a fix touches a constant/flag that appears in more than one place
  (grep for it), update all occurrences — don't fix one call site and leave
  a stale duplicate elsewhere.
- Always push each completed fix/feature immediately after verifying it
  (per the user's standing workflow preference) — don't batch unrelated
  changes into one commit.

## PLATFORM BAN HISTORY — never repeat these, on this bot or any future automation

Two real account bans have already happened on this project. Both are
"never do X" rules, not just bugs:

1. **Dual-webhook / dual-live-instance ban (Telegram, via HF+Render).**
   Running two deployed instances (two hosts, e.g. HF Space + Render, or
   primary + fallback HF Space) with the Telegram webhook effectively live
   on both at once got the bot account banned. Guardrail comments are
   already in place at `core.py` (module header), `app.py`
   (`_webhook_healer_task`, `startup()`), and `pages_deploy/_worker.js`
   (header) — read those before touching webhook/deploy-target code. Full
   writeup: `DEPLOYMENT.md`, `docs/DEPLOYMENT_SETUP.md`. **Rule: exactly
   one host may ever hold the live webhook for the production `BOT_TOKEN`
   at a time. Never add a "set webhook on a second host too, just in case"
   path.**

2. **Facebook automation ban (separate past project/feature).** Automating
   actions on Facebook (posting, scraping, auto-login, bulk messaging,
   etc. in a way that mimics a human or bypasses normal API use) got a past
   account banned. This repo currently has **no** Facebook integration —
   if one is ever added here (or to a sibling repo like `lms-repo`), it
   must go through Facebook's official Graph API with a registered app and
   documented permissions, never headless-browser automation of the
   facebook.com web UI, and must respect rate limits explicitly (no
   rapid-fire posting/DM loops). Treat any "automate Facebook" request as
   high-risk and confirm the exact approach with the user before writing
   code.

**General principle for any future platform integration (Telegram, FB, or
anything else):** before writing automation against a third-party
platform, check whether it (a) runs multiple live instances against one
account, or (b) mimics human UI actions instead of using an official API —
both are the two patterns that have already caused real bans here. When in
doubt, ask the user rather than assuming it's safe.

## When RAM-saving code is removed/restored for "no longer on Render" reasons

If a past commit removed a feature or shrank something *specifically to
save RAM on Render* (commit message will usually say so, e.g. "(RAM)"),
and the question of reverting it comes up, ask the user first — these are
product/feature decisions, not pure safety-limit tweaks. Pure numeric
limits (cache size, concurrency count, pixel caps) that are already
`IS_RENDER`-gated need no revert — they already give HF the full value.
