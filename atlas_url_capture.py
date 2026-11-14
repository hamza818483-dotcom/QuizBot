"""
atlas_url_capture.py — generic "send a link, get .mhtml + CSV" feature.

User sends ANY webpage URL as a plain text message (no command). The bot:
  1. Launches a headless browser, opens the URL, waits for it to settle.
  2. Captures the full rendered page as a real .mhtml snapshot (Chrome
     DevTools Protocol Page.captureSnapshot — same format Chrome produces
     via "Save As > Webpage, Single File").
  3. Sends that .mhtml file back to the user in DM.
  4. Feeds the same bytes into the existing parse_mhtml_to_mcqs /
     results_to_csv_bytes pipeline (atlas_mhtml.py) and, if MCQs are
     detected, sends the resulting .csv back too — fully automatic,
     no manual "save mhtml -> upload -> /qcsv" round-trip needed.

This does NOT use the chorcha.net login cookie (atlas_autoscrape.py) —
it's a plain anonymous page load, so it works for any public URL.
"""

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

PAGE_LOAD_TIMEOUT_MS = 30000
SETTLE_WAIT_MS = 1500


def is_plain_url(text: str) -> bool:
    """True iff the whole message is a single http(s) URL and nothing else."""
    return bool(text) and bool(URL_RE.match(text.strip()))


async def capture_url_as_mhtml(url: str) -> bytes:
    """Open `url` in a headless browser and return its full page as .mhtml bytes."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            # Kept in sync with atlas_rayvila.py -- see that file for the
            # full story. Short version: the SIGTRAP crash there survived
            # three different flag combos unchanged, with Chromium printing
            # nothing of its own before dying, which points to Crashpad's
            # (Chromium's crash-reporter) startup handshake being blocked by
            # the container rather than anything fixable via the sandbox/
            # process-model flags alone. --disable-crashpad-for-testing
            # added as the next (still unconfirmed) attempt; not yet
            # triggered in this single-page flow but the same risk applies.
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-crashpad-for-testing"],
        )
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)
            except Exception:
                # networkidle may never fire on pages with long-polling/websockets —
                # fall back to a looser wait so we still capture *something*.
                await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            await page.wait_for_timeout(SETTLE_WAIT_MS)

            cdp = await context.new_cdp_session(page)
            snapshot = await cdp.send("Page.captureSnapshot", {"format": "mhtml"})
            mhtml_text = snapshot["data"]
            return mhtml_text.encode("utf-8")
        finally:
            await browser.close()


async def process_url_to_mhtml_and_csv(url: str, progress_cb=None) -> dict:
    """
    Full pipeline for one URL: capture -> mhtml bytes -> (try) parse -> csv bytes.

    Returns {"mhtml_bytes": bytes, "file_name": str, "csv_bytes": bytes|None,
             "mcq_count": int, "error": str|None}
    progress_cb(stage: str) -- optional sync/async-ignored callback for status updates.
    """
    from atlas_mhtml import parse_mhtml_to_mcqs, results_to_csv_bytes

    def _notify(stage):
        if progress_cb:
            try:
                progress_cb(stage)
            except Exception:
                pass

    safe_name = re.sub(r"[^a-zA-Z0-9]+", "_", url.split("//", 1)[-1]).strip("_")[:60] or "page"
    file_name = f"{safe_name}.mhtml"

    _notify("fetching")
    mhtml_bytes = await capture_url_as_mhtml(url)

    result = {
        "mhtml_bytes": mhtml_bytes, "file_name": file_name,
        "csv_bytes": None, "mcq_count": 0, "error": None,
    }

    _notify("parsing")
    try:
        parsed = await asyncio.to_thread(parse_mhtml_to_mcqs, mhtml_bytes, file_name)
        results = parsed.get("results") or []
        if results:
            result["csv_bytes"] = results_to_csv_bytes(results)
            result["mcq_count"] = len(results)
    except Exception as e:
        logger.warning(f"[url-capture] parse/csv failed for {url}: {e}")
        result["error"] = str(e)

    return result
