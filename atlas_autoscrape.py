"""
atlas_autoscrape.py — /auto command backend.

User gives a sequential list of steps, one per line after /auto:

  - Plain label            -> click that single element (as before)
  - Multiple sub-steps, COMMA-separated on one line
                            -> execute EACH in left-to-right order on the
                               same page. Each sub-step is either a click
                               label or an "input:" step (see below), so
                               you can mix them: "input:30,পরবর্তী" types
                               30 then clicks "পরবর্তী". Comma is used
                               (not space) because a single label like
                               "সাধারণ জ্ঞান" itself contains a space.
  - "input:<value>"         -> type <value> into the first visible empty
                               text/number input on the page (e.g. "কয়টা
                               MCQ" field). Does not click anything.
  - "input:<label>=<value>" -> type <value> into the input field that is
                               nearest to/associated with <label> text
                               (use when a page has more than one input).

MULTIPLE RUNS IN ONE COMMAND: separate independent runs with a line
containing only "---". Each run starts fresh from the chorcha.net
homepage, executes its own steps, and produces its own separate CSV —
e.g. picking 3 different sub-topics under the same dropdown parent (which
auto-selects all children on click) as 3 clean single-topic exports
instead of one mixed run.

Before extracting, the final page is polled every ~1s for its MCQ-card
count; once the count is unchanged for 2 consecutive polls (or a max
wait is hit) the page is considered fully loaded, avoiding a partial
extract on slow-loading pages.

This module launches a Playwright browser, restores the chorcha.net login
session from a saved cookie (CHORCHA_TOKEN env var), then executes each
step in order (Subject -> Chapter -> Topic -> ... -> input -> Submit).
After the LAST step of each run, it waits for the page to settle and
returns that run's final page HTML, which is handed to
parse_mhtml_to_mcqs() (atlas_mhtml.py) for direct DOM-based MCQ extraction
— this is far more accurate than screenshot + AI-vision (exact
question/option text/order, correct answer detected from the
orange/green highlighted button, no OCR misreads).

This does NOT do auto-discovery of the menu tree — the user must supply
the exact click path. That keeps it reliable instead of guessing selectors.
"""

import asyncio
import os
import logging
from io import BytesIO

logger = logging.getLogger(__name__)

CHORCHA_BASE_URL = "https://chorcha.net"
CLICK_TIMEOUT_MS = 15000
SETTLE_WAIT_MS = 1500


def _build_cookies_from_token() -> list:
    """Builds a minimal Playwright cookie list from the CHORCHA_TOKEN env
    var. Only the auth token is required to restore a logged-in session —
    analytics/ad cookies from the user's original export are intentionally
    dropped (never stored), only the chorcha.net auth token is kept."""
    token = os.environ.get("CHORCHA_TOKEN", "").strip()
    if not token:
        return []
    return [{
        "name": "token",
        "value": token,
        "domain": ".chorcha.net",
        "path": "/",
        "httpOnly": False,
        "secure": False,
        "sameSite": "Lax",
    }]


class AutoScrapeError(Exception):
    pass


async def _expand_all_ai_explanations(page, per_click_wait_ms: int = 900, max_wait_ms: int = 6000):
    """
    "AI ব্যাখ্যা" button-এর content DOM-এ lazily load হয় (click না করলে
    fetch হয় না), তাই extraction-এর আগে প্রতিটা card-এর AI ব্যাখ্যা button
    খুঁজে click করে content load হওয়া পর্যন্ত অপেক্ষা করে।
    """
    try:
        buttons = page.locator("button:has-text('AI ব্যাখ্যা')")
        count = await buttons.count()
    except Exception:
        return
    for i in range(count):
        try:
            btn = buttons.nth(i)
            expanded = await btn.get_attribute("aria-expanded")
            if expanded == "true":
                continue
            await btn.click(timeout=5000)
            # Wait briefly for the AI-generated text to populate.
            await page.wait_for_timeout(per_click_wait_ms)
        except Exception:
            continue  # one failed AI-explanation shouldn't break the whole extraction


async def _wait_for_mcq_count_stable(page, poll_ms: int = 1000, max_wait_ms: int = 15000):
    """
    Slow-loading pages may still be adding MCQ cards to the DOM after
    navigation "settles". Poll the visible card count every `poll_ms`;
    once it's unchanged for 2 consecutive polls (or `max_wait_ms` is hit),
    treat the page as fully loaded.
    """
    card_selector = "div.border.rounded-xl"
    elapsed = 0
    last_count = -1
    stable_polls = 0
    while elapsed < max_wait_ms:
        try:
            count = await page.locator(card_selector).count()
        except Exception:
            break
        if count == last_count:
            stable_polls += 1
            if stable_polls >= 2:
                break
        else:
            stable_polls = 0
        last_count = count
        await page.wait_for_timeout(poll_ms)
        elapsed += poll_ms


async def _type_into_input(page, step_index, total, spec: str):
    """
    spec is the text after "input:" prefix.
    - "value"        -> type into first visible empty/enabled text|number input
    - "label=value"  -> type into the input nearest the given label text
    """
    if "=" in spec:
        label, value = spec.split("=", 1)
        label, value = label.strip(), value.strip()
        label_locator = page.get_by_text(label, exact=False).first
        try:
            await label_locator.wait_for(state="visible", timeout=CLICK_TIMEOUT_MS)
        except Exception:
            raise AutoScrapeError(
                f"ধাপ {step_index}/{total}: input label \"{label}\" পাওয়া যায়নি।"
            )
        # Look for an input inside the same container, else fall back to
        # the nearest following input in the DOM.
        container = label_locator.locator(
            "xpath=ancestor::*[self::div or self::label or self::li][1]"
        )
        input_locator = container.locator("input, textarea").first
        try:
            await input_locator.wait_for(state="visible", timeout=3000)
        except Exception:
            input_locator = label_locator.locator(
                "xpath=following::input[1] | following::textarea[1]"
            ).first
            await input_locator.wait_for(state="visible", timeout=CLICK_TIMEOUT_MS)
    else:
        value = spec.strip()
        input_locator = page.locator(
            "input:not([type=hidden]):not([disabled]), textarea:not([disabled])"
        ).first
        try:
            await input_locator.wait_for(state="visible", timeout=CLICK_TIMEOUT_MS)
        except Exception:
            raise AutoScrapeError(
                f"ধাপ {step_index}/{total}: কোনো input field পাওয়া যায়নি এই page-এ।"
            )

    await input_locator.click(timeout=CLICK_TIMEOUT_MS)
    await input_locator.fill("")
    await input_locator.fill(value)
    await page.wait_for_timeout(300)


async def _run_single_sequence(page, lines: list, progress_cb, run_no: int, run_total: int) -> bytes:
    """Executes one run's click/input steps starting from the chorcha.net
    homepage (page must already be there), then returns that run's final
    page HTML."""
    total = len(lines)
    for i, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line:
            continue

        if progress_cb:
            label = line if run_total == 1 else f"[run {run_no}/{run_total}] {line}"
            await progress_cb(i, total, label)

        # --- comma-separated sub-steps: each can be "input:..." or
        # a plain click label. Executed strictly in left-to-right
        # order (e.g. "input:30,Next Topic" types then clicks).
        sub_steps = [s.strip() for s in line.split(",") if s.strip()]
        for sub in sub_steps:
            if sub.lower().startswith("input:"):
                spec = sub[len("input:"):]
                await _type_into_input(page, i, total, spec)
                await page.wait_for_timeout(300)
                continue

            locator = page.get_by_text(sub, exact=True).first
            try:
                await locator.wait_for(state="visible", timeout=CLICK_TIMEOUT_MS)
            except Exception:
                raise AutoScrapeError(
                    f"রান {run_no}/{run_total}, ধাপ {i}/{total}: \"{sub}\" নামে কোনো button/link পাওয়া যায়নি এই page-এ। "
                    f"বানান/স্পেসিং ঠিক আছে কিনা চেক করুন।"
                )
            await locator.click(timeout=CLICK_TIMEOUT_MS)
            await page.wait_for_timeout(400)  # small gap between multi-selects

        await page.wait_for_timeout(SETTLE_WAIT_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # some steps are pure client-side, no network wait needed

    # Wait for MCQ cards to finish loading (slow pages keep adding cards
    # after navigation "settles"), then expand AI ব্যাখ্যা before grabbing HTML.
    await _wait_for_mcq_count_stable(page)
    await _expand_all_ai_explanations(page)

    html = await page.content()
    return html.encode("utf-8")


async def run_auto_click_sequence(
    labels: list,
    progress_cb=None,
) -> list:
    """
    Launches a browser, restores session. `labels` may contain one or more
    runs separated by a line containing only "---" -- each run starts
    fresh from the chorcha.net homepage and executes its own steps.

    Returns a list of HTML byte-strings, one per run (in order), each for
    direct DOM-based MCQ extraction via parse_mhtml_to_mcqs() — no
    screenshot / AI-vision needed.

    progress_cb(step_index, total_steps, label) -- optional async callback
    for live status updates in Telegram.
    """
    cookies = _build_cookies_from_token()
    if not cookies:
        raise AutoScrapeError(
            "CHORCHA_TOKEN সেট করা নেই। প্রথমে cookie token env var-এ সেভ করতে হবে।"
        )

    # Split into runs on a line containing only "---"
    runs = [[]]
    for raw_line in labels:
        if raw_line.strip() == "---":
            runs.append([])
        else:
            runs[-1].append(raw_line)
    runs = [r for r in runs if any(l.strip() for l in r)]
    if not runs:
        raise AutoScrapeError("কোনো ধাপ পাওয়া যায়নি।")

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu", "--single-process"]
        )
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            await context.add_cookies(cookies)
            page = await context.new_page()

            html_results = []
            run_total = len(runs)
            for run_no, run_lines in enumerate(runs, 1):
                # Fresh start for every run (independent topic pick).
                await page.goto(CHORCHA_BASE_URL, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(SETTLE_WAIT_MS)
                html = await _run_single_sequence(page, run_lines, progress_cb, run_no, run_total)
                html_results.append(html)

            return html_results
        finally:
            await browser.close()
