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
import re
import time
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


async def _expand_all_ai_explanations(page, per_click_wait_ms: int = 300, max_wait_ms: int = 5000,
                                       concurrency: int = 25, progress_cb=None, run_no: int = 1, run_total: int = 1):
    """
    দুই ধরনের ব্যাখ্যা button-ই DOM-এ collapsed/dropdown অবস্থায় থাকে --
    "ব্যাখ্যা" (pre-rendered content, শুধু dropdown খুলতে হয়) এবং
    "AI ব্যাখ্যা" (content lazily fetch হয়, click না করলে DOM-এই আসে না)।
    দুইটাই খোলা এবং populate হওয়া নিশ্চিত না করলে extraction-এ miss হয়ে
    যায়।

    Speed + accuracy balance:
    - Plain "ব্যাখ্যা" (content already in DOM): batch-expanded instantly,
      single pass, no wait.
    - "AI ব্যাখ্যা" (lazy-fetch): clicked in concurrent batches of
      `concurrency` instead of sequential (~15x faster for large banks).
      Poll tick shortened to 500ms for quicker detection of populated
      content (less time wasted per button once it's actually ready).
    - Final verification sweep: after the batch pass, every AI button is
      re-checked once more; anything still empty gets one more click +
      wait. This guarantees no explanation is silently left blank just
      because it was slow during its batch window.
    """
    try:
        all_buttons = page.locator("button:has-text('ব্যাখ্যা')")
        count = await all_buttons.count()
    except Exception:
        return
    if count == 0:
        return

    if progress_cb:
        try:
            await progress_cb(
                0, count,
                f"[run {run_no}/{run_total}] {count}টা ব্যাখ্যা বাটন পাওয়া গেছে, খোলা শুরু হচ্ছে...",
            )
        except Exception:
            pass

    # ---- Pass 1: classify + instantly batch-expand plain "ব্যাখ্যা" ----
    # (those whose content is already in DOM, just needs a click/attr flip)
    # Classification signal priority:
    #   1. Text label ("AI ব্যাখ্যা" vs plain "ব্যাখ্যা") -- MOST reliable.
    #      Both button variants render a leading icon, so icon-presence
    #      cannot distinguish them (was misclassifying "AI ব্যাখ্যা" as
    #      plain, causing lazy-fetch content to be silently skipped).
    #   2. Background color (solid dark green vs lighter/outlined) as
    #      fallback only if label text is unavailable/empty.
    async def _classify_and_expand(i):
        try:
            btn = all_buttons.nth(i)
            label = ""
            try:
                label = (await btn.inner_text()) or ""
            except Exception:
                pass

            if label:
                is_ai = "AI" in label
            else:
                is_ai = None
                try:
                    bg = await btn.evaluate("el => getComputedStyle(el).backgroundColor")
                    nums = re.findall(r"[\d.]+", bg or "")
                    if len(nums) >= 3:
                        r, g, b = float(nums[0]), float(nums[1]), float(nums[2])
                        is_ai = g > r + 15 and g > b + 15 and g < 130
                except Exception:
                    pass
                if is_ai is None:
                    is_ai = False  # last resort default: treat as plain

            if is_ai:
                return i
            expanded = await btn.get_attribute("aria-expanded")
            if expanded != "true":
                await btn.click(timeout=5000)
        except Exception:
            pass
        return None

    ai_indices = []
    classify_concurrency = max(concurrency, 40)
    done_classify = 0
    for batch_start in range(0, count, classify_concurrency):
        batch = list(range(batch_start, min(batch_start + classify_concurrency, count)))
        results = await asyncio.gather(*[_classify_and_expand(i) for i in batch])
        ai_indices.extend([r for r in results if r is not None])
        done_classify += len(batch)
        if progress_cb:
            try:
                await progress_cb(
                    done_classify, count,
                    f"[run {run_no}/{run_total}] ব্যাখ্যা বাটন যাচাই হচ্ছে... {done_classify}/{count}টা বাটন (প্রতি MCQ-তে ১-২টা বাটন থাকতে পারে)",
                )
            except Exception:
                pass

    # ---- Pass 2: concurrent batches for "AI ব্যাখ্যা" (lazy-fetch) ----
    async def _click_and_wait(idx):
        try:
            btn = all_buttons.nth(idx)
            expanded = await btn.get_attribute("aria-expanded")
            if expanded != "true":
                await btn.click(timeout=5000)
            elapsed = 0
            while elapsed < max_wait_ms:
                await page.wait_for_timeout(per_click_wait_ms)
                elapsed += per_click_wait_ms
                try:
                    handle = await btn.element_handle()
                    has_text = await page.evaluate(
                        """(el) => {
                            // The button's real wrapper on chorcha.net is
                            // <section class="...">, which holds ONLY the
                            // button until content is lazily fetched after
                            // click -- so checking the section's own text
                            // length (button label excluded) is the exact
                            // signal for "populated". closest('div') was
                            // unreliable: it could match an unrelated
                            // ancestor div and miss/false-positive.
                            let section = el.closest('section');
                            if (!section) return false;
                            let text = section.innerText || '';
                            // subtract the button's own label so we don't
                            // count "AI ব্যাখ্যা" itself as content
                            let btnText = el.innerText || '';
                            return (text.length - btnText.length) > 15;
                        }""",
                        handle,
                    )
                except Exception:
                    has_text = False
                if has_text:
                    return True
                if elapsed == per_click_wait_ms * 2:
                    try:
                        still = await btn.get_attribute("aria-expanded")
                        if still != "true":
                            await btn.click(timeout=5000)
                    except Exception:
                        pass
            return False
        except Exception:
            return False

    async def _click_and_wait_long(idx):
        """Same populated-check as _click_and_wait but with a much longer
        wait budget (15s) and a fresh click -- for the rare AI ব্যাখ্যা
        whose backend response (Groq/Gemini call on chorcha.net's side) is
        genuinely slower than the standard max_wait_ms."""
        try:
            btn = all_buttons.nth(idx)
            try:
                await btn.click(timeout=5000)
            except Exception:
                pass
            long_wait_ms, long_max_ms = 500, 15000
            elapsed = 0
            while elapsed < long_max_ms:
                await page.wait_for_timeout(long_wait_ms)
                elapsed += long_wait_ms
                try:
                    handle = await btn.element_handle()
                    has_text = await page.evaluate(
                        """(el) => {
                            let section = el.closest('section');
                            if (!section) return false;
                            let text = section.innerText || '';
                            let btnText = el.innerText || '';
                            return (text.length - btnText.length) > 15;
                        }""",
                        handle,
                    )
                except Exception:
                    has_text = False
                if has_text:
                    return True
            return False
        except Exception:
            return False

    unresolved = []
    total_ai = len(ai_indices)
    done_ai = 0
    for batch_start in range(0, len(ai_indices), concurrency):
        batch = ai_indices[batch_start:batch_start + concurrency]
        results = await asyncio.gather(*[_click_and_wait(idx) for idx in batch])
        for idx, ok in zip(batch, results):
            if not ok:
                unresolved.append(idx)
        done_ai += len(batch)
        if progress_cb and total_ai:
            try:
                await progress_cb(
                    done_ai, total_ai,
                    f"[run {run_no}/{run_total}] AI ব্যাখ্যা খোলা হচ্ছে... {done_ai}/{total_ai}",
                )
            except Exception:
                pass

    # ---- Final verification sweep: give unresolved buttons one more,
    # focused (lower concurrency, same wait) chance so nothing is
    # silently left blank because it was slow during its batch window.
    if unresolved:
        logger.warning(f"[/auto] {len(unresolved)}/{len(ai_indices)} AI ব্যাখ্যা needed a retry sweep")
        still_bad = []
        retry_concurrency = max(3, concurrency // 3)
        for batch_start in range(0, len(unresolved), retry_concurrency):
            batch = unresolved[batch_start:batch_start + retry_concurrency]
            results = await asyncio.gather(*[_click_and_wait(idx) for idx in batch])
            for idx, ok in zip(batch, results):
                if not ok:
                    still_bad.append(idx)
        if still_bad:
            logger.warning(f"[/auto] {len(still_bad)} AI ব্যাখ্যা still unpopulated after retry sweep: indices {still_bad}")
            # Final chance: some buttons are just slower than the standard
            # max_wait_ms budget (e.g. cold Groq/Gemini API call on
            # chorcha.net's backend) -- give these specific ones one more
            # attempt with a much longer wait before giving up for good.
            still_bad_2 = []
            for idx in still_bad:
                ok = await _click_and_wait_long(idx)
                if not ok:
                    still_bad_2.append(idx)
            if still_bad_2:
                logger.warning(f"[/auto] {len(still_bad_2)} AI ব্যাখ্যা STILL unpopulated after long-wait final attempt: indices {still_bad_2}")
            else:
                logger.info(f"[/auto] long-wait final attempt resolved all remaining AI ব্যাখ্যা")


async def _click_eye_icon_reveal(page, progress_cb=None, run_no: int = 1, run_total: int = 1):
    """
    কিছু chorcha.net পেজে (যেমন /read/... রিভিউ পেজ) সব প্রশ্নের উত্তর/ব্যাখ্যা
    সেকশন page-level "eye" টগল বাটন (data-event="eye_icon_review") ক্লিক না
    করা পর্যন্ত DOM-এ style="display: none;" অবস্থায় লুকানো থাকে -- prefix
    টা page-wide, প্রতিটা MCQ card-এর ভেতরের আলাদা "ব্যাখ্যা" বাটনের থেকে
    সম্পূর্ণ ভিন্ন লেয়ার। এটা ক্লিক না করলে _expand_all_ai_explanations
    কোনো ব্যাখ্যা বাটনই খুঁজে পাবে না কারণ সেগুলো hidden wrapper-এর ভেতরে।

    বাটনটা ঐচ্ছিক (সব পেজে থাকে না), তাই না পাওয়া গেলে চুপচাপ skip করে।
    """
    try:
        eye_btn = page.locator('button[data-event="eye_icon_review"]')
        count = await eye_btn.count()
    except Exception:
        return
    if count == 0:
        return  # এই পেজে eye-toggle নেই, স্বাভাবিক -- আগে থেকেই visible হতে পারে

    try:
        btn = eye_btn.first
        await btn.scroll_into_view_if_needed(timeout=5000)
        await btn.click(timeout=5000)
        # ক্লিকের পর reveal হওয়া হিডেন সেকশনগুলো DOM-এ বসতে সামান্য সময় লাগে
        await page.wait_for_timeout(600)
        if progress_cb:
            try:
                await progress_cb(0, 0, f"[run {run_no}/{run_total}] eye আইকন ক্লিক করে উত্তর/ব্যাখ্যা reveal করা হলো")
            except Exception:
                pass
        logger.info("[/auto] eye_icon_review button clicked -- answers/explanations revealed")
    except Exception as e:
        logger.warning(f"[/auto] eye_icon_review click failed (non-fatal, continuing): {e}")


async def _wait_for_mcq_count_stable(page, progress_cb=None, run_no=1, run_total=1, poll_ms: int = 1000, max_wait_ms: int = 240000):
    """
    Some pages (e.g. প্রশ্নব্যাংক browse) lazy-load MCQ cards only as the
    user scrolls down (viewport-based), while others (e.g. the post-submit
    review page) load everything immediately without any scroll.

    Chorcha.net prints the literal text "আর কোনো প্রশ্ন নেই" once every MCQ
    has been loaded -- this is the ONLY reliable end-of-list signal. A
    naive "count unchanged for 2 polls" heuristic falsely triggers during
    a normal network pause between batches (e.g. loading stalls briefly
    after 750/967 cards), truncating results. So: keep scrolling + polling
    until that end marker appears, or until `max_wait_ms` is hit as a
    last-resort safety cap (raised well above normal load time since large
    banks can take a while).

    If `progress_cb` is given, emits a live "কতটা MCQ পাওয়া গেছে" update on
    every poll so the person can see loading progress in real time, and
    (if the page shows an expected total, e.g. "৯৬৭ টি প্রশ্ন") the running
    count against that target.
    """
    end_marker = "আর কোনো প্রশ্ন নেই"
    card_selector = "div.border.rounded-xl"
    elapsed = 0
    last_count = -1
    stale_polls = 0  # consecutive polls with literally zero new cards AND no end marker yet
    expected_total = None

    async def _try_read_expected_total():
        try:
            text = await page.evaluate("() => document.body.innerText")
        except Exception:
            return None
        # Require the stronger/more specific anchor ("X টি প্রশ্ন" or "X টা
        # প্রশ্ন") rather than a bare "...প্রশ্ন" match, since a loose match
        # can pick up an unrelated small number elsewhere on the page
        # (e.g. an input field's placeholder or an unrelated count). If
        # multiple matches exist, take the LARGEST -- the true total for
        # a long list is never smaller than a coincidental match.
        matches = re.findall(r"([০-৯0-9]+)\s*(?:টি|টা)\s*প্রশ্ন", text)
        if not matches:
            return None
        bn_to_en = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
        values = []
        for digits in matches:
            try:
                values.append(int(digits.translate(bn_to_en)))
            except ValueError:
                continue
        return max(values) if values else None

    last_scroll_y = {"v": 0}
    while elapsed < max_wait_ms:
        try:
            new_y = await page.evaluate("""
                async (fromY) => {
                    // Smaller step (half a viewport, floor 250px) and a longer
                    // per-step pause than before -- a fast, large jump can
                    // leap past an IntersectionObserver-based lazy loader's
                    // trigger zone before it fires for cards in that zone,
                    // silently skipping a whole chunk of MCQs (confirmed via
                    // a real page where ~11 consecutive questions were never
                    // rendered because of exactly this). Finer, slower steps
                    // give every card's trigger zone time to be entered.
                    const step = Math.max(250, window.innerHeight * 0.5);
                    let y = fromY;
                    const target = document.body.scrollHeight;
                    while (y < target) {
                        y = Math.min(y + step, target);
                        window.scrollTo(0, y);
                        await new Promise(r => setTimeout(r, 150));
                    }
                    window.scrollTo(0, document.body.scrollHeight);
                    return window.scrollY;
                }
            """, last_scroll_y["v"])
            if isinstance(new_y, (int, float)):
                last_scroll_y["v"] = new_y
        except Exception:
            pass
        try:
            body_text = await page.evaluate("() => document.body.innerText")
        except Exception:
            body_text = ""

        try:
            count = await page.locator(card_selector).count()
        except Exception:
            break

        if expected_total is None or (expected_total < count):
            # Either not found yet, or a stale/wrong smaller reading from
            # an earlier poll -- re-check and never let it undercut what
            # we've already visibly counted.
            fresh_total = await _try_read_expected_total()
            if fresh_total and fresh_total >= count:
                expected_total = fresh_total
            elif expected_total is not None and expected_total < count:
                expected_total = None  # was wrong; stop showing a bogus target

        if progress_cb:
            target_str = f"/{expected_total}" if expected_total else ""
            try:
                await progress_cb(
                    count, expected_total or max(count, 1),
                    f"[run {run_no}/{run_total}] MCQ লোড হচ্ছে... {count}{target_str} টা পাওয়া গেছে",
                )
            except Exception:
                pass

        if end_marker in body_text:
            logger.info(f"[/auto] MCQ list end marker found after {elapsed}ms, count={count}")
            last_count = count
            break

        # NOTE: intentionally NOT stopping just because count reached
        # expected_total -- the visible card count can hit the target
        # before the page has actually appended the real end-of-list
        # marker text (lazy-render lag), so a count-only stop risks
        # shipping the CSV while some MCQs are still settling in. Only
        # the end_marker check above, or the stale-poll timeout below
        # (genuinely stuck page), end this loop.

        if count == last_count:
            stale_polls += 1
        else:
            stale_polls = 0
        last_count = count
        await page.wait_for_timeout(poll_ms)
        elapsed += poll_ms
        if stale_polls >= 45:
            # ~15s with zero growth AND no end-marker -- likely a page
            # that never shows the marker (e.g. a short list with no
            # "no more" footer). Stop here rather than burning the full
            # max_wait_ms budget.
            logger.info(f"[/auto] MCQ count stable at {last_count} for {stale_polls}s with no end marker, stopping")
            break
    else:
        # Loop exhausted max_wait_ms without hitting the marker or the
        # stale-polls cutoff -- likely still actively loading. Flag this
        # clearly rather than silently returning what we have.
        logger.warning(f"[/auto] MCQ wait hit max_wait_ms={max_wait_ms} without end marker, count={last_count}")
        if progress_cb:
            try:
                await progress_cb(
                    last_count, expected_total or last_count,
                    f"[run {run_no}/{run_total}] ⚠️ {max_wait_ms // 1000}s পার হয়ে গেছে, এখনো \"আর কোনো প্রশ্ন নেই\" পাওয়া যায়নি "
                    f"({last_count}টা পর্যন্ত পাওয়া গেছে) -- কিছু MCQ miss হতে পারে",
                )
            except Exception:
                pass

    # One more full top-to-bottom incremental pass before capture. The
    # wait loop above can finish (end marker found, count stable) even
    # though some individual card sections never actually rendered during
    # the live scroll -- a lazy-render/hydration timing gap distinct from
    # "page still loading". A second full re-walk gives any such gap
    # another chance to render before we grab the final HTML.
    pre_repass_count = last_count
    try:
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(150)
        await page.evaluate("""
            async () => {
                // Same slower/finer step as the main polling loop, for the
                // same reason: fast large jumps can skip a lazy loader's
                // trigger zone for a whole chunk of cards.
                const step = Math.max(250, window.innerHeight * 0.5);
                let y = 0;
                const target = document.body.scrollHeight;
                while (y < target) {
                    y = Math.min(y + step, target);
                    window.scrollTo(0, y);
                    await new Promise(r => setTimeout(r, 150));
                }
            }
        """)
        logger.info(f"[/auto] re-pass scroll completed without error (pre-count={pre_repass_count})")
    except Exception as e_repass:
        logger.warning(f"[/auto] re-pass scroll FAILED with exception: {e_repass}")

    try:
        recheck_count = await page.locator(card_selector).count()
        logger.info(f"[/auto] post-re-pass card count: {recheck_count} (was {pre_repass_count})")
        if recheck_count > last_count:
            logger.info(f"[/auto] re-pass caught {recheck_count - last_count} additional card(s) ({last_count} -> {recheck_count}) that the live scroll missed")
            last_count = recheck_count
            await page.wait_for_timeout(800)  # let any follow-on renders settle
    except Exception as e_recount:
        logger.warning(f"[/auto] post-re-pass count check FAILED: {e_recount}")

    # Scroll back to top so screenshots/DOM order reads naturally (no
    # functional effect on HTML extraction, just tidy).
    try:
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    return last_count, expected_total


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


async def _run_single_sequence(page, lines: list, progress_cb, run_no: int, run_total: int,
                                skip_from_line: int = 0, seed_processed_subs: list = None) -> bytes:
    """Executes one run's click/input steps starting from the chorcha.net
    homepage (page must already be there), then returns that run's final
    page HTML.

    skip_from_line: number of leading `lines` entries to SKIP clicking
    (the caller has already navigated the browser to that point via
    back-navigation, reusing a shared prefix with the previous run).
    seed_processed_subs: processed_subs history to pre-populate with for
    those skipped lines, so subject-context lookback (KNOWN_CATEGORY_CARD_URLS)
    still works correctly for the remaining steps.
    """
    total = len(lines)
    processed_subs = list(seed_processed_subs) if seed_processed_subs else []
    for i, raw_line in enumerate(lines, 1):
        if i <= skip_from_line:
            continue
        line = raw_line.strip()
        if not line:
            continue

        if progress_cb:
            label = line if run_total == 1 else f"[run {run_no}/{run_total}] {line}"
            await progress_cb(i, total, label)

        # --- comma-separated sub-steps: each can be "input:..." or
        # a plain click label. Executed strictly in left-to-right
        # order (e.g. "input:30,Next Topic" types then clicks).
        # A sub-step wrapped in double-quotes ("...") is taken AS-IS,
        # commas inside it are literal text (part of the button's own
        # label, e.g. "বিস্তার ও সংরক্ষণ, জীবের পরিবেশ") and are NOT
        # treated as step separators.
        sub_steps = []
        for part in re.findall(r'"[^"]*"|[^,]+', line):
            part = part.strip()
            if not part:
                continue
            if part.startswith('"') and part.endswith('"') and len(part) >= 2:
                part = part[1:-1].strip()
            if part:
                sub_steps.append(part)
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

            if "=" in sub and not sub.lower().startswith("input:"):
                # "লেখা=link" -> persist this label->URL mapping (D1,
                # survives bot restart) AND use it immediately for this
                # step, instead of clicking. Use for image-only/unmatchable
                # cards where text-matching can never find the element.
                _label_part, _url_part = sub.split("=", 1)
                _label_part, _url_part = _label_part.strip(), _url_part.strip()
                if _url_part.startswith("http"):
                    try:
                        from core import auto_link_map_set
                        await auto_link_map_set(_label_part, _url_part, context=current_subject or "")
                    except Exception as e:
                        logger.warning(f"[/auto] auto_link_map_set failed for {_label_part!r}: {e}")
                    try:
                        await page.goto(_url_part, wait_until="networkidle", timeout=30000)
                        await page.wait_for_timeout(300)
                        processed_subs.append(_label_part)
                        continue
                    except Exception:
                        raise AutoScrapeError(
                            f"ধাপ {i}/{total}: link \"{_url_part}\"-এ যাওয়া যায়নি।"
                        )

            # Permanent from-page+label -> target-URL cache, populated
            # automatically after any successful match (see below). Check
            # this BEFORE any DOM matching -- if a previous run (even
            # before a bot restart) already resolved this exact button on
            # this exact page, skip straight to navigating there instead
            # of re-running the whole exact/JS/partial/fuzzy chain.
            _cache_from_url = page.url
            try:
                from core import auto_click_cache_get
                _cached_target = await auto_click_cache_get(_cache_from_url, sub)
            except Exception:
                _cached_target = None
            if _cached_target:
                try:
                    await page.goto(_cached_target, wait_until="networkidle", timeout=30000)
                    await page.wait_for_timeout(300)
                    # Sanity-check: confirm the cached target actually
                    # looks like a valid landing (not an error/blank page
                    # from a stale entry saved during an earlier broken
                    # run) before trusting it -- a cheap body-text length
                    # check catches empty/error pages without needing to
                    # re-verify the exact label is present (the label may
                    # legitimately not repeat on the destination page).
                    try:
                        _body_len = await page.evaluate("() => document.body.innerText.length")
                    except Exception:
                        _body_len = 999  # evaluate failed -- don't block on this check
                    if _body_len < 20:
                        raise RuntimeError(f"cached target looks empty/broken (body length {_body_len})")
                    processed_subs.append(sub)
                    logger.info(f"[/auto] step {i}/{total} sub={sub!r}: matched via click-cache -> {_cached_target}")
                    continue
                except Exception as e:
                    logger.warning(f"[/auto] click-cache goto failed for {sub!r}, falling back to normal matching: {e}")
                    # fall through to normal matching below -- cache entry
                    # might be stale (page structure changed); don't trust
                    # it blindly if navigating there actually fails.

            match_method = None
            locator = page.get_by_text(sub, exact=True).first
            try:
                await locator.wait_for(state="visible", timeout=CLICK_TIMEOUT_MS)
                match_method = "exact-text"
            except Exception:
                locator = None
                # Result/CTA pages (e.g. a "retake exam" button after
                # submit) sometimes finish their score-calculation
                # animation and inject the button into the DOM well AFTER
                # networkidle already fired -- a single extra settle
                # wasn't always enough (observed real-world timeouts on
                # "পুনরায় পরীক্ষা দাও"). Poll repeatedly instead of one
                # extra attempt: up to ~25s total, re-checking every 2s,
                # so slow post-submit animations are caught without
                # slowing down the common case where the element was
                # already there (loop exits immediately on first match).
                poll_elapsed_ms = 0
                poll_max_ms = 25000
                poll_step_ms = 2000
                while poll_elapsed_ms < poll_max_ms:
                    await page.wait_for_timeout(poll_step_ms)
                    poll_elapsed_ms += poll_step_ms
                    try:
                        await page.wait_for_load_state("networkidle", timeout=3000)
                    except Exception:
                        pass
                    locator = page.get_by_text(sub, exact=True).first
                    try:
                        await locator.wait_for(state="visible", timeout=3000)
                        match_method = "exact-text (after poll wait)"
                        break
                    except Exception:
                        locator = None

            # First-attempt-vs-retake label alias: chorcha.net swaps a
            # button's own label depending on exam state -- e.g. an exam
            # never attempted before shows "পরীক্ষা দাও", but the SAME
            # button/position shows "পুনরায় পরীক্ষা দাও" only AFTER at
            # least one attempt exists. If the user wrote the "পুনরায়"
            # variant but this run is hitting the exam for the first
            # time (or vice versa), the exact-text search above
            # legitimately finds nothing -- the literal label just isn't
            # on the page. Strip a leading "পুনরায় " and retry once with
            # the bare label before falling through to the generic
            # JS/partial fallbacks.
            if locator is None and sub.startswith("পুনরায় "):
                alias = sub[len("পুনরায় "):].strip()
                if alias:
                    alias_locator = page.get_by_text(alias, exact=True).first
                    try:
                        await alias_locator.wait_for(state="visible", timeout=3000)
                        locator = alias_locator
                        match_method = f"exact-text (পুনরায়-alias -> {alias!r})"
                        logger.info(
                            f"[/auto] step {i}/{total} sub={sub!r}: literal label not found "
                            f"(likely first-attempt state), matched first-attempt alias {alias!r} instead"
                        )
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

            if locator is None and element_handle is None and len(sub) >= 2:
                # Fallback: filter/tag-chip buttons often render as
                # "লেবেল<count>" combined in one clickable element (e.g.
                # "গদ্য 1526" as seen in a subject-filter bar) -- an exact
                # match on the bare label never succeeds because the
                # element's real text includes a trailing number. Try a
                # regex match: label followed by optional whitespace and
                # a number, anchored at the start of the element's text.
                try:
                    element_handle = await page.evaluate_handle(
                        """(target) => {
                            const norm = s => (s || '').normalize('NFC').replace(/\\s+/g, ' ').trim();
                            const wanted = norm(target);
                            const re = new RegExp('^' + wanted.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&') + '\\\\s*[0-9০-৯]*');
                            const all = Array.from(document.querySelectorAll('button, a, [data-event], [role="button"], div, span, li'));
                            let best = null, bestLen = Infinity;
                            for (const el of all) {
                                if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE') continue;
                                const txt = norm(el.textContent);
                                if (re.test(txt) && txt.length < bestLen) {
                                    best = el; bestLen = txt.length;
                                }
                            }
                            return best;
                        }""",
                        sub,
                    )
                    is_null = await page.evaluate("(h) => h === null", element_handle)
                    if is_null:
                        element_handle = None
                    else:
                        match_method = "label-plus-count-chip"
                        logger.info(f"[/auto] step {i}/{total} sub={sub!r}: matched via label+count chip pattern")
                except Exception:
                    element_handle = None

            if locator is None and element_handle is None and len(sub) >= 2:
                # Fallback: partial/keyword match. Only used as a last
                # resort when nothing matched exactly -- picks the
                # SMALLEST element (by text length) whose normalized text
                # contains `sub` as a substring, to reduce the chance of
                # grabbing an oversized ancestor/wrong sibling when the
                # label appears in multiple places. If several elements
                # tie at the same smallest length, none is picked (too
                # ambiguous to guess safely).
                # A trailing "..."/"…" in the user's given label is a
                # truncation marker, not literal text -- strip it so
                # "বিস্তার ও সংরক্ষণ, জী..." matches the same way as
                # "বিস্তার ও সংরক্ষণ, জী" against the real button text.
                sub_for_match = re.sub(r'(\.\.\.|…)\s*$', '', sub).strip() or sub
                try:
                    element_handle = await page.evaluate_handle(
                        """(target) => {
                            const norm = s => (s || '').normalize('NFC').replace(/\\s+/g, ' ').trim();
                            const wanted = norm(target);
                            const all = Array.from(document.querySelectorAll('button, a, [data-event], [role="button"], div, span, li'));
                            let best = null, bestLen = Infinity;
                            for (const el of all) {
                                if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE') continue;
                                const ev = el.getAttribute && el.getAttribute('data-event');
                                const txt = norm(ev || el.textContent);
                                if (!txt.includes(wanted)) continue;
                                // On a length tie, prefer the element deeper in the
                                // DOM tree (more specific / less likely to be an
                                // oversized wrapper) -- an ancestor and its only
                                // meaningful child often report identical
                                // normalized textContent length, and previously
                                // this ambiguity caused the match to be dropped
                                // entirely instead of picking the more specific one.
                                if (txt.length < bestLen) {
                                    best = el; bestLen = txt.length;
                                } else if (txt.length === bestLen && best && el !== best && best.contains(el)) {
                                    best = el; // el is a descendant of current best -- more specific, prefer it
                                }
                            }
                            return best;
                        }""",
                        sub_for_match,
                    )
                    is_null = await page.evaluate("(h) => h === null", element_handle)
                    if is_null:
                        element_handle = None
                    else:
                        match_method = "partial-keyword"
                        logger.info(f"[/auto] step {i}/{total} sub={sub!r}: no exact match, used partial/keyword match")
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
                        try:
                            await page.goto(known_url, wait_until="domcontentloaded", timeout=30000)
                        except Exception as e_first:
                            logger.warning(f"[/auto] known-url-map goto failed for {sub!r} (attempt 1): {e_first}, retrying once")
                            await page.goto(known_url, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(1200)
                        processed_subs.append(sub)
                        continue
                    except Exception as e:
                        logger.warning(f"[/auto] known-url-map goto failed for {sub!r} (both attempts): {e}")

            if locator is None and element_handle is None:
                # Fallback: previously-saved persistent label->URL mapping
                # (set via a "লেখা=link" step in an earlier run; survives
                # bot restart). Checked after the static in-code map so
                # user-taught mappings extend it without a code change.
                try:
                    from core import auto_link_map_get
                    saved_url = await auto_link_map_get(sub, context=current_subject or "")
                except Exception:
                    saved_url = None
                if saved_url:
                    try:
                        logger.info(
                            f"[/auto] step {i}/{total} sub={sub!r}: matched via saved link-map -> {saved_url}"
                        )
                        await page.goto(saved_url, wait_until="networkidle", timeout=30000)
                        await page.wait_for_timeout(300)
                        processed_subs.append(sub)
                        continue
                    except Exception as e:
                        logger.warning(f"[/auto] saved link-map goto failed for {sub!r}: {e}")

            if locator is None and element_handle is None and len(sub) >= 2:
                # Fallback: fuzzy/near-match for minor spelling mistakes
                # (e.g. "গতীবিদ্যা" typed instead of "গতিবিদ্যা"). Collects
                # every clickable candidate's normalized text, then picks
                # the one with the smallest Levenshtein edit-distance to
                # the wanted label -- but only if it's a clear winner
                # (closest match is unambiguously better than the next
                # closest, and the distance is small relative to the
                # label's length) to avoid accidentally clicking the wrong
                # button on a genuinely-wrong or ambiguous label.
                try:
                    candidates = await page.evaluate(
                        """() => {
                            const norm = s => (s || '').normalize('NFC').replace(/\\s+/g, ' ').trim();
                            const all = Array.from(document.querySelectorAll('button, a, [data-event], [role="button"], div, span, li'));
                            const seen = new Set();
                            const out = [];
                            for (const el of all) {
                                const t = norm(el.textContent);
                                if (!t || t.length > 60 || seen.has(t)) continue;
                                seen.add(t);
                                out.push(t);
                            }
                            return out;
                        }"""
                    )
                except Exception:
                    candidates = []

                def _lev(a, b):
                    if a == b:
                        return 0
                    la, lb = len(a), len(b)
                    if la == 0:
                        return lb
                    if lb == 0:
                        return la
                    prev = list(range(lb + 1))
                    for ia in range(1, la + 1):
                        cur = [ia] + [0] * lb
                        for ib in range(1, lb + 1):
                            cost = 0 if a[ia - 1] == b[ib - 1] else 1
                            cur[ib] = min(prev[ib] + 1, cur[ib - 1] + 1, prev[ib - 1] + cost)
                        prev = cur
                    return prev[lb]

                wanted_norm = unicodedata.normalize("NFC", sub).strip()
                scored = sorted(
                    ((c, _lev(wanted_norm, c)) for c in candidates),
                    key=lambda x: x[1],
                )
                fuzzy_target = None
                if scored:
                    best_text, best_dist = scored[0]
                    # Allow up to ~25% of label length as edit distance,
                    # minimum tolerance of 1 char, and require the runner-up
                    # (if any) to be clearly worse so an ambiguous near-tie
                    # doesn't silently click the wrong card.
                    max_allowed = max(1, len(wanted_norm) // 4)
                    second_dist = scored[1][1] if len(scored) > 1 else None
                    if best_dist <= max_allowed and best_dist > 0 and (
                        second_dist is None or second_dist > best_dist
                    ):
                        fuzzy_target = best_text

                if fuzzy_target:
                    try:
                        element_handle = await page.evaluate_handle(
                            """(target) => {
                                const norm = s => (s || '').normalize('NFC').replace(/\\s+/g, ' ').trim();
                                const wanted = norm(target);
                                const all = Array.from(document.querySelectorAll('button, a, [data-event], [role="button"], div, span, li'));
                                let best = null, bestLen = Infinity;
                                for (const el of all) {
                                    if (norm(el.textContent) === wanted && el.textContent.length < bestLen) {
                                        best = el; bestLen = el.textContent.length;
                                    }
                                }
                                return best;
                            }""",
                            fuzzy_target,
                        )
                        is_null = await page.evaluate("(h) => h === null", element_handle)
                        if is_null:
                            element_handle = None
                        else:
                            match_method = "fuzzy-spelling"
                            logger.info(
                                f"[/auto] step {i}/{total} sub={sub!r}: no exact/partial match, "
                                f"fuzzy-matched to {fuzzy_target!r}"
                            )
                    except Exception:
                        element_handle = None

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

            # Save this successful (from-page, label) -> target-url match
            # to the permanent click-cache so future runs skip matching
            # entirely for this exact button. Best-effort -- never let a
            # cache-write failure interrupt the actual scrape.
            try:
                from core import auto_click_cache_set
                await auto_click_cache_set(_cache_from_url, sub, page.url)
            except Exception as e:
                logger.warning(f"[/auto] auto_click_cache_set failed for {sub!r}: {e}")

        await page.wait_for_timeout(SETTLE_WAIT_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # some steps are pure client-side, no network wait needed

    # Wait for MCQ cards to finish loading (slow pages keep adding cards
    # after navigation "settles"), then expand AI ব্যাখ্যা before grabbing HTML.
    await _wait_for_mcq_count_stable(page, progress_cb=progress_cb, run_no=run_no, run_total=run_total)
    await _click_eye_icon_reveal(page, progress_cb=progress_cb, run_no=run_no, run_total=run_total)
    _diag_card_selector = "div.border.rounded-xl"
    try:
        pre_expand_count = await page.locator(_diag_card_selector).count()
        logger.info(f"[/auto] card count before AI ব্যাখ্যা expansion: {pre_expand_count}")
    except Exception:
        pre_expand_count = None
    await _expand_all_ai_explanations(page, progress_cb=progress_cb, run_no=run_no, run_total=run_total)
    try:
        post_expand_count = await page.locator(_diag_card_selector).count()
        logger.info(f"[/auto] card count after AI ব্যাখ্যা expansion: {post_expand_count} (was {pre_expand_count})")
        if pre_expand_count is not None and post_expand_count < pre_expand_count:
            logger.warning(f"[/auto] CARD COUNT DROPPED during AI ব্যাখ্যা expansion: {pre_expand_count} -> {post_expand_count} (lost {pre_expand_count - post_expand_count})")
    except Exception:
        pass

    # FINAL VERIFICATION PASS: right before capturing HTML, do one more
    # full top-to-bottom scroll + settle. _wait_for_mcq_count_stable's own
    # re-pass runs BEFORE AI-explanation expansion, which can itself
    # trigger further lazy-render/layout shifts that reveal a few more
    # cards lower on the page -- this catches exactly that gap so cards
    # near the end of a long list are never silently missing from the
    # final captured HTML.
    try:
        pre_final_count = await page.locator(_diag_card_selector).count()
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(150)
        await page.evaluate("""
            async () => {
                const step = Math.max(250, window.innerHeight * 0.5);
                let y = 0;
                const target = document.body.scrollHeight;
                while (y < target) {
                    y = Math.min(y + step, target);
                    window.scrollTo(0, y);
                    await new Promise(r => setTimeout(r, 150));
                }
            }
        """)
        await page.wait_for_timeout(1000)  # let any late renders settle
        post_final_count = await page.locator(_diag_card_selector).count()
        if post_final_count > pre_final_count:
            logger.warning(f"[/auto] FINAL verification pass caught {post_final_count - pre_final_count} more card(s) ({pre_final_count} -> {post_final_count}) after AI ব্যাখ্যা expansion -- would have been missing without this pass")
            await page.wait_for_timeout(800)
        else:
            logger.info(f"[/auto] final verification pass: no change ({pre_final_count} cards, confirmed stable)")
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception as e_final:
        logger.warning(f"[/auto] final verification pass failed (non-fatal): {e_final}")

    html = await page.content()
    _parser_selector = "div[class*='rounded-xl'][class*='p-5'], div[class*='rounded-xl'][class*='pb-6']"
    try:
        final_card_count = await page.locator(_parser_selector).count()
        logger.info(f"[/auto] HTML captured with {final_card_count} card-like divs in final DOM (run {run_no}/{run_total})")
    except Exception:
        final_card_count = None

    # EXTRA SAFETY LOOP (additive -- doesn't remove/alter anything above):
    # re-check using the EXACT selector parse_mhtml_to_mcqs() itself uses
    # (the diagnostic selectors above use a looser "div.border.rounded-xl"
    # match, which can under/over-count vs the real parser). If one more
    # scroll pass still turns up MORE matching cards than what we just
    # captured, that means content was still settling -- re-capture the
    # HTML again (up to 2 extra tries) and keep whichever capture has the
    # most cards, so a late-rendering card near the end of a long list
    # can never end up missing from the final CSV.
    best_html, best_count = html, (final_card_count or 0)
    for _retry in range(2):
        try:
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(150)
            await page.evaluate("""
                async () => {
                    const step = Math.max(250, window.innerHeight * 0.5);
                    let y = 0;
                    const target = document.body.scrollHeight;
                    while (y < target) {
                        y = Math.min(y + step, target);
                        window.scrollTo(0, y);
                        await new Promise(r => setTimeout(r, 150));
                    }
                }
            """)
            await page.wait_for_timeout(1200)
            recheck_count = await page.locator(_parser_selector).count()
            if recheck_count > best_count:
                logger.warning(f"[/auto] SAFETY RETRY {_retry+1}: found {recheck_count} cards (was {best_count}) -- recapturing HTML")
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(150)
                new_html = await page.content()
                best_html, best_count = new_html, recheck_count
            else:
                logger.info(f"[/auto] safety retry {_retry+1}: no improvement ({recheck_count} cards), stopping retries")
                break
        except Exception as e_retry:
            logger.warning(f"[/auto] safety retry {_retry+1} failed (non-fatal): {e_retry}")
            break
    html = best_html
    try:
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass

    # TAG-BASED VERIFICATION (the real bug-catching check): a plain card
    # COUNT can look "stable" even when a whole chunk of real MCQs failed
    # to render and the page just ends up with fewer total cards overall
    # (not a positional gap, since chorcha.net doesn't renumber -- it's
    # simply short by however many failed to load). Each real MCQ card
    # carries a stable "tag-cyan" span (e.g. "Din.B 25", "SB 23") that
    # uniquely identifies its source question, present ONLY on real MCQ
    # cards (never on parent/passage-only cards). Counting these tags is a
    # far more reliable signal than raw card count, since it's immune to
    # the parent/context-card accounting confusion entirely. Confirmed on
    # a real page: the full manually-saved page had 240 tagged cards, the
    # bot's own capture had only 226 -- 14 real MCQs missing despite the
    # total *card* count elsewhere looking self-consistent.
    async def _count_tagged_cards():
        try:
            return await page.evaluate("""
                () => {
                    const cards = document.querySelectorAll(
                        "div[class*='rounded-xl'][class*='p-5'], div[class*='rounded-xl'][class*='pb-6']"
                    );
                    let n = 0;
                    for (const c of cards) {
                        if (c.querySelector("span[class*='tag-cyan']")) n++;
                    }
                    return n;
                }
            """)
        except Exception:
            return None

    async def _read_serial_numbers():
        # Universal check (doesn't depend on tag-cyan spans existing) --
        # reads each card's own leading number and returns only TOP-LEVEL
        # ones (skips nested "10.1"/"10.2" style, which aren't part of the
        # main sequence). If the page's own numbering has a gap (e.g.
        # ...23, 24, 36... skipping 25-35), that means real questions were
        # never rendered into the DOM at all.
        try:
            raw = await page.evaluate("""
                () => {
                    const cards = document.querySelectorAll(
                        "div[class*='rounded-xl'][class*='p-5'], div[class*='rounded-xl'][class*='pb-6']"
                    );
                    const out = [];
                    for (const c of cards) {
                        const qd = c.querySelector("div[class*='font-medium']");
                        if (!qd) { continue; }
                        const t = qd.textContent || "";
                        const m = t.match(/^\\s*([0-9\\u09E6-\\u09EF]+)\\s*[\\.\\)\\-\\u0983:]/);
                        if (m) out.push(m[1]);
                    }
                    return out;
                }
            """)
        except Exception:
            return []
        bn2en = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
        nums = []
        for n in (raw or []):
            if "." in n:
                continue  # nested sub-question, not part of main sequence
            try:
                nums.append(int(n.translate(bn2en)))
            except ValueError:
                pass
        return nums

    async def _serial_gap_report():
        nums = await _read_serial_numbers()
        gaps = [(a, b) for a, b in zip(nums, nums[1:]) if b - a > 1]
        return gaps, len(nums)

    prev_tag_count = await _count_tagged_cards()
    prev_gaps, _ = await _serial_gap_report()
    for _tag_retry in range(3):
        if prev_tag_count is None and not prev_gaps:
            break
        try:
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(200)
            await page.evaluate("""
                async () => {
                    // Slow, fine-grained scroll -- last-resort pass
                    // specifically to give a lazy loader every chance to
                    // fire for cards that were missed the first time.
                    const step = Math.max(150, window.innerHeight * 0.3);
                    let y = 0;
                    const target = document.body.scrollHeight;
                    while (y < target) {
                        y = Math.min(y + step, target);
                        window.scrollTo(0, y);
                        await new Promise(r => setTimeout(r, 220));
                    }
                }
            """)
            await page.wait_for_timeout(1500)
        except Exception as e_tag:
            logger.warning(f"[/auto] tag-verification scroll pass failed (non-fatal): {e_tag}")
            break
        new_tag_count = await _count_tagged_cards()
        if new_tag_count is None:
            break
        if new_tag_count > prev_tag_count:
            logger.warning(f"[/auto] TAG-VERIFICATION: found MORE real MCQ cards on retry {_tag_retry+1} ({prev_tag_count} -> {new_tag_count}) -- recapturing HTML (would have been missing without this pass)")
            prev_tag_count = new_tag_count
            html = await page.content()
        else:
            logger.info(f"[/auto] tag-verification stable at {new_tag_count} real MCQ card(s) after retry {_tag_retry+1}, stopping")
            break
    else:
        logger.warning(f"[/auto] tag-verification count still changing after all retries (run {run_no}/{run_total}) -- proceeding with best available capture ({prev_tag_count} tagged cards)")

    return html.encode("utf-8"), processed_subs


async def run_auto_click_sequence(
    labels: list,
    progress_cb=None,
    on_run_complete=None,
) -> list:
    """
    Launches a browser, restores session. `labels` may contain one or more
    runs separated by a line containing only "---" -- each run executes
    its own steps. When a run shares a leading sequence of identical steps
    with the immediately previous run (typical for the "OldName>NewName"
    sibling-topic shorthand, which only changes ONE step near the end),
    the shared prefix is NOT re-clicked from the homepage again -- instead
    the browser navigates back exactly enough steps to land back at the
    divergence point, then only the new/changed steps from there onward
    are clicked. This is both faster and avoids redundant navigation.

    Returns a list of HTML byte-strings, one per run (in order), each for
    direct DOM-based MCQ extraction via parse_mhtml_to_mcqs() — no
    screenshot / AI-vision needed.

    progress_cb(step_index, total_steps, label) -- optional async callback
    for live status updates in Telegram.

    on_run_complete(html_bytes, run_no, run_total) -- optional async
    callback fired immediately after each run's HTML is captured, so the
    caller can parse+send that run's CSV right away instead of waiting
    for every run to finish first.
    """
    cookies = _build_cookies_from_token()
    if not cookies:
        raise AutoScrapeError(
            "CHORCHA_TOKEN সেট করা নেই। প্রথমে cookie token env var-এ সেভ করতে হবে।"
        )

    # Split into runs on a line containing only "---"
    runs = [[]]
    for raw_line in labels:
        if raw_line.strip() in ("---", "***"):
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
            prev_run_lines = None  # the previous run's raw `lines` list, for prefix comparison
            prev_processed_subs = []  # processed_subs history at the end of the previous run
            for run_no, run_lines in enumerate(runs, 1):
                common_prefix = 0
                if prev_run_lines is not None:
                    for a, b in zip(prev_run_lines, run_lines):
                        if a.strip() == b.strip():
                            common_prefix += 1
                        else:
                            break

                # NOTE: previously this reused a shared prefix with
                # go_back() instead of re-clicking from the homepage.
                # go_back() replays browser history, which does not
                # reliably restore in-page JS/DOM state on this SPA --
                # divergent-step clicks after a go_back() chain could land
                # on stale state and fail to find the target button even
                # though the same label works fine as a fresh direct run.
                # Always restart fresh from the homepage and re-click the
                # full step sequence for every run; slower per-run but
                # matches the reliability of a standalone /auto run.
                await page.goto(CHORCHA_BASE_URL, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(SETTLE_WAIT_MS)

                html, processed_subs_this_run = await _run_single_sequence(
                    page, run_lines, progress_cb, run_no, run_total,
                    skip_from_line=0, seed_processed_subs=[],
                )
                html_results.append(html)
                prev_run_lines = run_lines
                prev_processed_subs = processed_subs_this_run

                if on_run_complete:
                    try:
                        await on_run_complete(html, run_no, run_total)
                    except Exception as e:
                        logger.warning(f"[/auto] on_run_complete callback failed for run {run_no}: {e}")

            return html_results
        finally:
            await browser.close()
