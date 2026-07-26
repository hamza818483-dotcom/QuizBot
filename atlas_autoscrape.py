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
                    f"[run {run_no}/{run_total}] ব্যাখ্যা বাটন যাচাই হচ্ছে... {done_classify}/{count}",
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


async def _wait_for_mcq_count_stable(page, progress_cb=None, run_no=1, run_total=1, poll_ms: int = 1000, max_wait_ms: int = 120000):
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

    while elapsed < max_wait_ms:
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
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

        if expected_total and count >= expected_total:
            logger.info(f"[/auto] MCQ count reached expected_total={expected_total} (count={count}), stopping without waiting for end marker")
            last_count = count
            break

        if count == last_count:
            stale_polls += 1
        else:
            stale_polls = 0
        last_count = count
        await page.wait_for_timeout(poll_ms)
        elapsed += poll_ms
        if stale_polls >= 15:
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
                        await auto_link_map_set(_label_part, _url_part)
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

            if locator is None and element_handle is None and len(sub) >= 2:
                # Fallback: partial/keyword match. Only used as a last
                # resort when nothing matched exactly -- picks the
                # SMALLEST element (by text length) whose normalized text
                # contains `sub` as a substring, to reduce the chance of
                # grabbing an oversized ancestor/wrong sibling when the
                # label appears in multiple places. If several elements
                # tie at the same smallest length, none is picked (too
                # ambiguous to guess safely).
                try:
                    element_handle = await page.evaluate_handle(
                        """(target) => {
                            const norm = s => (s || '').normalize('NFC').replace(/\\s+/g, ' ').trim();
                            const wanted = norm(target);
                            const all = Array.from(document.querySelectorAll('button, a, [data-event], [role="button"], div, span, li'));
                            let best = null, bestLen = Infinity, tie = false;
                            for (const el of all) {
                                if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE') continue;
                                const ev = el.getAttribute && el.getAttribute('data-event');
                                const txt = norm(ev || el.textContent);
                                if (!txt.includes(wanted)) continue;
                                if (txt.length < bestLen) {
                                    best = el; bestLen = txt.length; tie = false;
                                } else if (txt.length === bestLen && el !== best) {
                                    tie = true;
                                }
                            }
                            return (best && !tie) ? best : null;
                        }""",
                        sub,
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
                        await page.goto(known_url, wait_until="networkidle", timeout=30000)
                        await page.wait_for_timeout(300)
                        processed_subs.append(sub)
                        continue
                    except Exception as e:
                        logger.warning(f"[/auto] known-url-map goto failed for {sub!r}: {e}")

            if locator is None and element_handle is None:
                # Fallback: previously-saved persistent label->URL mapping
                # (set via a "লেখা=link" step in an earlier run; survives
                # bot restart). Checked after the static in-code map so
                # user-taught mappings extend it without a code change.
                try:
                    from core import auto_link_map_get
                    saved_url = await auto_link_map_get(sub)
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
    await _wait_for_mcq_count_stable(page, progress_cb=progress_cb, run_no=run_no, run_total=run_total)
    await _expand_all_ai_explanations(page, progress_cb=progress_cb, run_no=run_no, run_total=run_total)

    html = await page.content()
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

                if common_prefix > 0:
                    # Reuse the shared prefix: navigate back exactly enough
                    # steps to undo everything the previous run did AFTER
                    # the divergence point, instead of restarting from the
                    # homepage and re-clicking identical steps.
                    steps_to_undo = len(prev_run_lines) - common_prefix
                    logger.info(
                        f"[/auto] run {run_no}/{run_total}: reusing {common_prefix} shared step(s) "
                        f"with previous run, going back {steps_to_undo} step(s) instead of restarting"
                    )
                    ok = True
                    for _ in range(steps_to_undo):
                        try:
                            await page.go_back(wait_until="networkidle", timeout=15000)
                            await page.wait_for_timeout(300)
                        except Exception as e:
                            logger.warning(f"[/auto] go_back failed mid-way, falling back to full restart: {e}")
                            ok = False
                            break
                    if not ok:
                        common_prefix = 0  # fall through to full restart below

                if common_prefix == 0:
                    # Fresh start (first run, or previous run's steps
                    # diverge immediately / back-navigation failed).
                    await page.goto(CHORCHA_BASE_URL, wait_until="networkidle", timeout=30000)
                    await page.wait_for_timeout(SETTLE_WAIT_MS)
                    seed_subs = []
                else:
                    seed_subs = prev_processed_subs[:common_prefix]

                html, processed_subs_this_run = await _run_single_sequence(
                    page, run_lines, progress_cb, run_no, run_total,
                    skip_from_line=common_prefix, seed_processed_subs=seed_subs,
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
