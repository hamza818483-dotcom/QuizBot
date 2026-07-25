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
import unicodedata
from io import BytesIO

logger = logging.getLogger(__name__)

CHORCHA_BASE_URL = "https://chorcha.net"
CLICK_TIMEOUT_MS = 15000
SETTLE_WAIT_MS = 1500

# Some category cards (একাডেমিক / মূলবই / মেডিকেল / ভার্সিটি 'ক') render as a
# single flattened image with the label baked in, AND their internal URL
# slug has no derivable relation to the Bengali label or to each other
# (e.g. একাডেমিক -> hsc-chemistry-1st-test-paper, not chem_1-academic) --
# so neither text-matching nor data-event-matching can ever find them.
# Known label -> URL mappings go here, keyed by subject context (the last
# preceding line before these labels in the user's step list, e.g.
# "রসায়ন ১ম পত্র"). Extend this table as more subjects are reported.
KNOWN_CATEGORY_CARD_URLS = {
    "রসায়ন ১ম পত্র": {
        "একাডেমিক": "https://chorcha.net/question-bank/hsc-chemistry-1st-test-paper",
        "মূলবই": "https://chorcha.net/question-bank/chem_1-mainbook",
        "মেডিকেল": "https://chorcha.net/read-archive/chem_1-medical",
        "ভার্সিটি 'ক'": "https://chorcha.net/read-archive/chem_1-versity",
        "MCQ": "https://chorcha.net/question-bank/hsc-chemistry-1st-paper-mcq",
    },
    "জীববিজ্ঞান ১ম পত্র": {
        "একাডেমিক": "https://chorcha.net/question-bank/hsc-biology-1st-test-paper",
        "মূলবই": "https://chorcha.net/question-bank/bio_1-mainbook",
        "মেডিকেল": "https://chorcha.net/read-archive/bio_1-medical",
        "ভার্সিটি 'ক'": "https://chorcha.net/read-archive/bio_1-versity",
        "MCQ": "https://chorcha.net/question-bank/hsc-biology-1st-paper-mcq",
    },
    "জীববিজ্ঞান ২য় পত্র": {
        "একাডেমিক": "https://chorcha.net/question-bank/hsc-biology-2nd-test-paper",
        "মূলবই": "https://chorcha.net/question-bank/bio_2-mainbook",
        "মেডিকেল": "https://chorcha.net/read-archive/bio_2-medical",
        "ভার্সিটি 'ক'": "https://chorcha.net/read-archive/bio_2-versity",
        "MCQ": "https://chorcha.net/question-bank/hsc-biology-2nd-paper-mcq",
    },
    "উচ্চতর গণিত ১ম পত্র": {
        "একাডেমিক": "https://chorcha.net/question-bank/hsc-higher-math-1st-test-paper",
        "মূলবই": "https://chorcha.net/question-bank/math_1-mainbook",
        "মেডিকেল": "https://chorcha.net/read-archive/math_1-medical",
        "ভার্সিটি 'ক'": "https://chorcha.net/read-archive/math_1-versity",
        "MCQ": "https://chorcha.net/question-bank/hsc-higher-math-1st-paper-mcq",
    },
    "উচ্চতর গণিত ২য় পত্র": {
        "একাডেমিক": "https://chorcha.net/question-bank/hsc-higher-math-2nd-test-paper",
        "মূলবই": "https://chorcha.net/question-bank/math_2-mainbook",
        "মেডিকেল": "https://chorcha.net/read-archive/math_2-medical",
        "ভার্সিটি 'ক'": "https://chorcha.net/read-archive/math_2-versity",
        "MCQ": "https://chorcha.net/question-bank/hsc-higher-math-2nd-paper-mcq",
    },
    "পদার্থবিজ্ঞান ১ম পত্র": {
        "একাডেমিক": "https://chorcha.net/question-bank/hsc-physics-1st-test-paper",
        "মূলবই": "https://chorcha.net/question-bank/phys_1-mainbook",
        "মেডিকেল": "https://chorcha.net/read-archive/phys_1-medical",
        "ভার্সিটি 'ক'": "https://chorcha.net/read-archive/phys_1-versity",
        "MCQ": "https://chorcha.net/question-bank/hsc-physics-1st-paper-mcq",
    },
    "পদার্থবিজ্ঞান ২য় পত্র": {
        "একাডেমিক": "https://chorcha.net/question-bank/hsc-physics-2nd-test-paper",
        "মূলবই": "https://chorcha.net/question-bank/phys_2-mainbook",
        "মেডিকেল": "https://chorcha.net/read-archive/phys_2-medical",
        "ভার্সিটি 'ক'": "https://chorcha.net/read-archive/phys_2-versity",
        "MCQ": "https://chorcha.net/question-bank/hsc-physics-2nd-paper-mcq",
    },
}
KNOWN_CATEGORY_CARD_URLS = {
    unicodedata.normalize("NFC", k): {
        unicodedata.normalize("NFC", k2): v2 for k2, v2 in v.items()
    }
    for k, v in KNOWN_CATEGORY_CARD_URLS.items()
}


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


async def _wait_for_mcq_count_stable(page, poll_ms: int = 1000, max_wait_ms: int = 20000):
    """
    Some pages (e.g. প্রশ্নব্যাংক browse) lazy-load MCQ cards only as the
    user scrolls down (viewport-based), while others (e.g. the post-submit
    review page) load everything immediately without any scroll. To cover
    both: scroll to the bottom on every poll (harmless no-op if the page
    doesn't need it) and watch the MCQ-card count; once it's unchanged for
    2 consecutive polls (or `max_wait_ms` is hit), treat the page as fully
    loaded.
    """
    card_selector = "div.border.rounded-xl"
    elapsed = 0
    last_count = -1
    stable_polls = 0
    while elapsed < max_wait_ms:
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
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

    # Scroll back to top so screenshots/DOM order reads naturally (no
    # functional effect on HTML extraction, just tidy).
    try:
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
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


async def _run_single_sequence(page, lines: list, progress_cb, run_no: int, run_total: int) -> bytes:
    """Executes one run's click/input steps starting from the chorcha.net
    homepage (page must already be there), then returns that run's final
    page HTML."""
    total = len(lines)
    processed_subs = []  # every sub-step string processed so far, in order (for subject-lookback)
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
            sub = unicodedata.normalize("NFC", sub)
            # Recompute subject context every step by scanning everything
            # processed so far in this run (stateless -- avoids relying on
            # a single click-succeeded code path to update it).
            current_subject = next(
                (p for p in reversed(processed_subs) if p in KNOWN_CATEGORY_CARD_URLS),
                None,
            )

            if sub.lower().startswith("input:"):
                spec = sub[len("input:"):]
                await _type_into_input(page, i, total, spec)
                await page.wait_for_timeout(300)
                continue

            if sub.lower().startswith("goto:"):
                # Direct URL navigation -- use when a card's label has no
                # matchable DOM text or data-event (e.g. একাডেমিক/মূলবই/
                # মেডিকেল/ভার্সিটি category cards, whose internal English
                # slug bears no derivable relation to the Bengali label,
                # so text/attribute matching can never find them).
                url = sub[len("goto:"):].strip()
                if not url.startswith("http"):
                    url = CHORCHA_BASE_URL.rstrip("/") + "/" + url.lstrip("/")
                try:
                    await page.goto(url, wait_until="networkidle", timeout=30000)
                except Exception:
                    raise AutoScrapeError(
                        f"রান {run_no}/{run_total}, ধাপ {i}/{total}: URL \"{url}\"-এ যাওয়া যায়নি।"
                    )
                await page.wait_for_timeout(300)
                continue

            match_method = None
            locator = page.get_by_text(sub, exact=True).first
            try:
                await locator.wait_for(state="visible", timeout=CLICK_TIMEOUT_MS)
                match_method = "exact-text"
            except Exception:
                locator = None

            element_handle = None  # used for the JS-normalized fallback (bypasses locator)
            if locator is None:
                # Fallback A+B combined, JS-side: Bengali text can be
                # represented in two different Unicode forms for the same
                # visible glyph (e.g. "য়" as one precomposed codepoint
                # U+09DF, or as "য"+nukta two codepoints) -- visually
                # identical but byte-different. CSS attribute selectors
                # and XPath string comparisons do NOT Unicode-normalize,
                # so they silently fail to match across these forms even
                # though Python's own unicodedata.normalize("NFC", ...)
                # was applied to `sub`. Do the comparison inside the page
                # via JS's String.normalize("NFC"), which normalizes both
                # sides consistently.
                try:
                    element_handle = await page.evaluate_handle(
                        """(target) => {
                            const norm = s => (s || '').normalize('NFC').replace(/\\s+/g, ' ').trim();
                            const wanted = norm(target);
                            const all = Array.from(document.querySelectorAll('*'));
                            // Prefer an element whose OWN data-event attribute
                            // ends with "_<label>" (image-only cards).
                            for (const el of all.reverse()) {
                                const ev = el.getAttribute && el.getAttribute('data-event');
                                if (ev && norm(ev).endsWith('_' + wanted)) return el;
                            }
                            // Fallback: element whose full text content
                            // (own + descendants) equals the label, e.g.
                            // a card title split across sibling spans.
                            for (const el of all) {
                                if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE') continue;
                                if (norm(el.textContent) === wanted) return el;
                            }
                            return null;
                        }""",
                        sub,
                    )
                    is_null = await page.evaluate("(h) => h === null", element_handle)
                    if is_null:
                        element_handle = None
                    else:
                        match_method = "js-normalized"
                except Exception:
                    element_handle = None

            if locator is None and element_handle is None:
                # Fallback: this exact label under the current subject
                # is a known image-only card whose URL has no derivable
                # relation to the label (see KNOWN_CATEGORY_CARD_URLS) --
                # navigate straight there instead of clicking.
                subj_map = KNOWN_CATEGORY_CARD_URLS.get(current_subject or "", {})
                known_url = subj_map.get(sub)
                if known_url:
                    try:
                        logger.info(
                            f"[/auto] step {i}/{total} sub={sub!r}: matched via known-url-map "
                            f"(subject={current_subject!r}) -> {known_url}"
                        )
                        await page.goto(known_url, wait_until="networkidle", timeout=30000)
                        await page.wait_for_timeout(300)
                        processed_subs.append(sub)
                        continue
                    except Exception as e:
                        logger.warning(f"[/auto] known-url-map goto failed for {sub!r}: {e}")

            if locator is None and element_handle is None:
                logger.error(
                    f"[/auto] step {i}/{total} sub={sub!r} NOT FOUND. "
                    f"subject_context={current_subject!r}, "
                    f"processed_so_far={processed_subs}, "
                    f"known_subjects_in_map={list(KNOWN_CATEGORY_CARD_URLS.keys())}"
                )
                raise AutoScrapeError(
                    f"রান {run_no}/{run_total}, ধাপ {i}/{total}: \"{sub}\" নামে কোনো button/link পাওয়া যায়নি এই page-এ। "
                    f"বানান/স্পেসিং ঠিক আছে কিনা চেক করুন।"
                )
            logger.info(f"[/auto] step {i}/{total} sub={sub!r}: matched via {match_method}")
            if locator is not None:
                await locator.click(timeout=CLICK_TIMEOUT_MS)
            else:
                await element_handle.as_element().scroll_into_view_if_needed()
                await element_handle.as_element().click(timeout=CLICK_TIMEOUT_MS)
            await page.wait_for_timeout(400)  # small gap between multi-selects
            processed_subs.append(sub)

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
