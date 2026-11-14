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

## When RAM-saving code is removed/restored for "no longer on Render" reasons

If a past commit removed a feature or shrank something *specifically to
save RAM on Render* (commit message will usually say so, e.g. "(RAM)"),
and the question of reverting it comes up, ask the user first — these are
product/feature decisions, not pure safety-limit tweaks. Pure numeric
limits (cache size, concurrency count, pixel caps) that are already
`IS_RENDER`-gated need no revert — they already give HF the full value.
