"""
atlas_autoscrape.py — /auto command backend.

User gives a sequential list of steps, one per line after /auto:

  - Plain label            -> click that single element (as before)
  - Multiple labels, COMMA-separated on one line ("Label A,Label B")
                            -> click EACH of them in sequence (multi-select
                               checkboxes/chips on the same page). Comma is
                               used (not space) because a single label like
                               "সাধারণ জ্ঞান" itself contains a space.
  - "input:<value>"         -> type <value> into the first visible empty
                               text/number input on the page (e.g. "কয়টা
                               MCQ" field). Does not click anything.
  - "input:<label>=<value>" -> type <value> into the input field that is
                               nearest to/associated with <label> text
                               (use when a page has more than one input).

This module launches a Playwright browser, restores the chorcha.net login
session from a saved cookie (CHORCHA_TOKEN env var), then executes each
step in order (Subject -> Chapter -> Topic -> ... -> input -> Submit).
After the LAST step, it waits for the page to settle and takes a full-page
screenshot, ready to be handed to the existing QBM AI-extraction pipeline
(same as any PDF/QBM page image).

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


async def run_auto_click_sequence(
    labels: list,
    progress_cb=None,
) -> bytes:
    """
    Launches a browser, restores session, clicks through `labels` in order,
    and returns PNG screenshot bytes of the final page.

    progress_cb(step_index, total_steps, label) -- optional async callback
    for live status updates in Telegram.
    """
    cookies = _build_cookies_from_token()
    if not cookies:
        raise AutoScrapeError(
            "CHORCHA_TOKEN সেট করা নেই। প্রথমে cookie token env var-এ সেভ করতে হবে।"
        )

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

            await page.goto(CHORCHA_BASE_URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(SETTLE_WAIT_MS)

            total = len(labels)
            for i, raw_line in enumerate(labels, 1):
                line = raw_line.strip()
                if not line:
                    continue

                if progress_cb:
                    await progress_cb(i, total, line)

                # --- input: step -------------------------------------------------
                if line.lower().startswith("input:"):
                    spec = line[len("input:"):]
                    await _type_into_input(page, i, total, spec)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    continue

                # --- click step(s): comma-separated = multi-select on same page --
                sub_labels = [s.strip() for s in line.split(",") if s.strip()]
                for label in sub_labels:
                    locator = page.get_by_text(label, exact=True).first
                    try:
                        await locator.wait_for(state="visible", timeout=CLICK_TIMEOUT_MS)
                    except Exception:
                        raise AutoScrapeError(
                            f"ধাপ {i}/{total}: \"{label}\" নামে কোনো button/link পাওয়া যায়নি এই page-এ। "
                            f"বানান/স্পেসিং ঠিক আছে কিনা চেক করুন।"
                        )
                    await locator.click(timeout=CLICK_TIMEOUT_MS)
                    await page.wait_for_timeout(400)  # small gap between multi-selects

                await page.wait_for_timeout(SETTLE_WAIT_MS)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass  # some steps are pure client-side, no network wait needed

            # Final settle before screenshot
            await page.wait_for_timeout(1000)
            screenshot_bytes = await page.screenshot(full_page=True, type="png")
            return screenshot_bytes
        finally:
            await browser.close()
