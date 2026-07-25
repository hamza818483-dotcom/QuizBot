"""
atlas_autoscrape.py — /auto command backend.

User gives a sequential list of button/link TEXT labels (one per line after
/auto). This module launches a Playwright browser, restores the chorcha.net
login session from a saved cookie (CHORCHA_TOKEN env var), then clicks
through each label in order (Subject -> Chapter -> Topic -> ...). After the
LAST label is clicked, it waits for the page to settle and takes a
full-page screenshot, ready to be handed to the existing QBM AI-extraction
pipeline (same as any PDF/QBM page image).

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
            for i, label in enumerate(labels, 1):
                label = label.strip()
                if not label:
                    continue
                if progress_cb:
                    await progress_cb(i, total, label)

                # Try to find any clickable element containing this exact
                # text (button, link, div, span -- chorcha.net menu items
                # aren't guaranteed to be a specific tag).
                locator = page.get_by_text(label, exact=True).first
                try:
                    await locator.wait_for(state="visible", timeout=CLICK_TIMEOUT_MS)
                except Exception:
                    raise AutoScrapeError(
                        f"ধাপ {i}/{total}: \"{label}\" নামে কোনো button/link পাওয়া যায়নি এই page-এ। "
                        f"বানান/স্পেসিং ঠিক আছে কিনা চেক করুন।"
                    )
                await locator.click(timeout=CLICK_TIMEOUT_MS)
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
