"""
atlas_rayvila.py — site-specific auto-scraper for rayvila.com exam links.

User gives a Rayvila exam link (e.g. https://www.rayvila.com/g.php/240831004923).
Unlike the generic atlas_url_capture.py (single page snapshot), Rayvila needs a
scripted click-through: login -> home -> confirm -> pre_test_preparation -> exam.
All questions/options/correct-answers are already present in exam.php's DOM as
hidden inputs, so we scrape via page.content() + BeautifulSoup/regex — no AI,
no need to even click Submit (step 5 is the reliable, primary data source; the
optional Submit -> view_result.php path is NOT implemented here since it's
explicitly not required per RAYVILA_TASK_NOTES.md).

Returns the same result shape as atlas_url_capture.process_url_to_mhtml_and_csv
expects for app.py's /api/url-to-csv and the Telegram DM flow:
    {"csv_bytes": bytes|None, "mcq_count": int, "error": str|None}
"""

import logging
import re

logger = logging.getLogger(__name__)

RAYVILA_RE = re.compile(r"rayvila\.com.*?/(?:g\.php|exam)", re.IGNORECASE)

PAGE_LOAD_TIMEOUT_MS = 30000
AD_SETTLE_WAIT_MS = 10000  # user's instruction: wait ~10s for ad banner to settle
PREP_PAGE_TIMEOUT_MS = 20000  # generous timeout for the unknown pre_test_preparation step
SUBMIT_SETTLE_WAIT_MS = 6000


def is_rayvila_url(text: str) -> bool:
    """True iff the text is/contains a rayvila.com exam link (/g.php/ or /exam/ path)."""
    return bool(text) and bool(RAYVILA_RE.search(text.strip()))


async def _try_close_popup(page):
    """Defensive: close any ad overlay/modal that might appear, with its own
    close ('×') button. Not seen in the captured reference flow, but the user
    mentioned it can happen in practice. Never let this raise/abort the run."""
    try:
        for sel in [
            "text=×", "text=X", ".close", ".modal-close",
            "[class*='close']", "[aria-label='Close']", "[aria-label='close']",
        ]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1000):
                    await el.click(timeout=1000)
                    await page.wait_for_timeout(300)
                    return
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"[rayvila] popup-close check skipped: {e}")


# Step name -> approximate progress % for live-dashboard callbacks.
RAYVILA_STEP_PCT = {
    "opening": 5,
    "login": 15,
    "home": 25,
    "waiting_ad": 35,
    "confirm": 55,
    "pre_test_preparation": 65,
    "exam": 80,
    "parsing": 90,
    "done": 100,
}


def _notify(progress_cb, step: str):
    if progress_cb:
        try:
            progress_cb(step, RAYVILA_STEP_PCT.get(step, 0))
        except Exception:
            pass


async def _goto_exam_page(page, url: str, candidate_name: str, progress_cb=None):
    """Scripted click-through from the given Rayvila URL (any step of the
    flow — login/home/confirm/exam) to exam.php, landing with the questions
    DOM loaded. Returns nothing; raises on unrecoverable failure (caller
    captures a screenshot/HTML dump for debugging).
    progress_cb(step: str, pct: int) -- optional callback for live-dashboard updates."""
    _notify(progress_cb, "opening")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
    except Exception:
        await page.goto(url, wait_until="load", timeout=PAGE_LOAD_TIMEOUT_MS)

    # ---- Step 1: login.php (only if we actually land there) ----
    if "login.php" in page.url:
        _notify(progress_cb, "login")
        try:
            name_input = page.locator("input#name")
            if await name_input.count() > 0:
                await name_input.fill(candidate_name)
            await page.locator("div#login_browser").click(timeout=10000)
            await page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        except Exception as e:
            logger.warning(f"[rayvila] login step issue (continuing): {e}")

    await _try_close_popup(page)

    # ---- Step 2: home.php -> wait for ad banner to settle, click "Start Test" ----
    if "home.php" in page.url:
        _notify(progress_cb, "home")
        _notify(progress_cb, "waiting_ad")
        await page.wait_for_timeout(AD_SETTLE_WAIT_MS)
        await _try_close_popup(page)
        try:
            start_btn = page.locator("a.res", has_text="Start Test")
            if await start_btn.count() == 0:
                start_btn = page.locator("a.res")
            await start_btn.first.click(timeout=10000)
            await page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        except Exception as e:
            logger.warning(f"[rayvila] 'Start Test' click issue (continuing): {e}")

    await _try_close_popup(page)

    # ---- Step 3: confirm.php -> click "Continue" ----
    if "confirm.php" in page.url:
        _notify(progress_cb, "confirm")
        try:
            cont_btn = page.locator("a.res2", has_text="Continue")
            if await cont_btn.count() == 0:
                cont_btn = page.locator("a.res2")
            await cont_btn.first.click(timeout=10000)
            await page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        except Exception as e:
            logger.warning(f"[rayvila] 'Continue' click issue (continuing): {e}")

    # ---- Step 4: pre_test_preparation.php -> unknown shape, handle both cases ----
    if "pre_test_preparation.php" in page.url:
        _notify(progress_cb, "pre_test_preparation")
        landed_on_exam = False
        try:
            await page.wait_for_url(re.compile(r"exam\.php"), timeout=PREP_PAGE_TIMEOUT_MS)
            landed_on_exam = True
        except Exception:
            pass

        if not landed_on_exam:
            try:
                await page.wait_for_load_state("networkidle", timeout=PREP_PAGE_TIMEOUT_MS)
            except Exception:
                pass

        if "exam.php" not in page.url:
            # Look for any plausible clickable continue control.
            for sel in ["a.res", "a.res2", "a.res3", ".res3", "button[type=submit]",
                        "input[type=submit]", "form a", "a"]:
                try:
                    cand = page.locator(sel).first
                    if await cand.count() > 0 and await cand.is_visible(timeout=1000):
                        await cand.click(timeout=5000)
                        await page.wait_for_load_state("domcontentloaded", timeout=PREP_PAGE_TIMEOUT_MS)
                        break
                except Exception:
                    continue

        if "exam.php" not in page.url:
            try:
                await page.wait_for_url(re.compile(r"exam\.php"), timeout=PREP_PAGE_TIMEOUT_MS)
            except Exception as e:
                # Fallback debug artifacts for this known-risky step.
                try:
                    html_dump = await page.content()
                    logger.error(
                        f"[rayvila] stuck on pre_test_preparation.php, could not reach "
                        f"exam.php. Current URL: {page.url}. HTML length: {len(html_dump)}."
                    )
                except Exception:
                    pass
                raise RuntimeError(
                    f"pre_test_preparation.php did not advance to exam.php (stuck at {page.url}): {e}"
                )

    if "exam.php" not in page.url:
        # One more generous wait in case of a slow redirect chain we haven't modeled.
        try:
            await page.wait_for_url(re.compile(r"exam\.php"), timeout=PREP_PAGE_TIMEOUT_MS)
        except Exception as e:
            raise RuntimeError(f"Never reached exam.php (stuck at {page.url}): {e}")

    _notify(progress_cb, "exam")


def _parse_exam_html(html: str) -> list:
    """Parse exam.php's rendered HTML into CSV-row dicts.

    For each <div class="tabcontent3" id="N">:
      - question text: first <p>, with leading "QN." prefix stripped
      - options: text after <label for="{N}{letter}"> up to next <p></p>/label
      - correct answer: value of <input name="rN" value="X"> (1-based: 1=A..4=D)
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    results = []

    blocks = soup.find_all("div", class_="tabcontent3")
    for block in blocks:
        block_id = block.get("id", "")
        if not block_id.isdigit():
            continue
        n = block_id

        p_tag = block.find("p")
        if not p_tag:
            continue
        q_text = p_tag.get_text(" ", strip=True)
        q_text = re.sub(rf"^Q\s*{re.escape(n)}\s*\.\s*", "", q_text, flags=re.IGNORECASE).strip()
        if not q_text:
            q_text = re.sub(r"^Q\s*\d+\s*\.\s*", "", q_text, flags=re.IGNORECASE).strip()

        letters = ["A", "B", "C", "D", "E"]
        option_texts = {letter: "" for letter in letters}

        for letter in letters:
            label = block.find("label", attrs={"for": f"{n}{letter.lower()}"})
            if not label:
                continue
            opt_parts = []
            node = label.next_sibling
            while node is not None:
                # Stop at the next label/radio (next option) or an empty <p></p> delimiter.
                name = getattr(node, "name", None)
                if name == "label":
                    break
                if name == "input" and node.get("type") == "radio":
                    break
                if name == "p":
                    text_here = node.get_text(strip=True)
                    if text_here == "":
                        node = node.next_sibling
                        break
                    opt_parts.append(text_here)
                    node = node.next_sibling
                    continue
                if hasattr(node, "get_text"):
                    opt_parts.append(node.get_text(" ", strip=True))
                else:
                    opt_parts.append(str(node).strip())
                node = node.next_sibling
            option_texts[letter] = " ".join(p for p in opt_parts if p).strip()

        answer_input = block.find("input", attrs={"name": f"r{n}"})
        answer_val = (answer_input.get("value", "") if answer_input else "").strip()

        if not q_text and not any(option_texts.values()):
            continue

        results.append({
            "questions": q_text,
            "option1": option_texts["A"],
            "option2": option_texts["B"],
            "option3": option_texts["C"],
            "option4": option_texts["D"],
            "option5": option_texts["E"] if option_texts["E"] else "",
            "answer": answer_val,
            "explanation": "",
            "type": 1,
            "section": 1,
        })

    # Keep deterministic order by numeric block id.
    def _sort_key(row_idx_pair):
        return row_idx_pair[0]

    try:
        ordered = sorted(
            zip((int(b.get("id")) for b in blocks if b.get("id", "").isdigit()), results[: len(blocks)]),
            key=_sort_key,
        )
        results = [r for _, r in ordered]
    except Exception:
        pass  # if anything about the zip/sort is off, keep insertion order — still correct data

    return results


async def scrape_rayvila_exam(url: str, candidate_name: str = "Student", progress_cb=None) -> dict:
    """Full pipeline: Playwright click-through to exam.php -> parse DOM -> CSV bytes.

    Returns {"csv_bytes": bytes|None, "mcq_count": int, "error": str|None}
    (same shape atlas_url_capture.process_url_to_mhtml_and_csv returns).
    progress_cb(step: str, pct: int) -- optional callback for live-dashboard updates;
    steps: opening, login, home, waiting_ad, confirm, pre_test_preparation, exam,
    parsing, done.
    """
    from playwright.async_api import async_playwright
    from atlas_mhtml import results_to_csv_bytes

    result = {"csv_bytes": None, "mcq_count": 0, "error": None}

    async with async_playwright() as pw:
        # RAM was confirmed NOT the cause (production host logged 10+GB free
        # right before an identical crash). Diagnostic kept instead: log
        # which Chromium binary Playwright will try to launch, to help spot
        # an install/version mismatch if this crash recurs.
        try:
            logger.info(f"[rayvila] chromium executable: {pw.chromium.executable_path}")
        except Exception:
            pass
        # Correction to the previous fix: removing --single-process alone
        # did NOT stop the crash (redeployed, same SIGTRAP, same signature,
        # on both the bundled chrome-headless-shell and the system-chromium
        # fallback -- confirmed from a fresh prod log). That rules out
        # --single-process as the cause: the signal fires identically with
        # or without it.
        #
        # SIGTRAP (not SIGKILL/SIGSEGV) immediately on launch is Chromium's
        # own CHECK()/DCHECK() trap, not an OOM kill or a generic segfault.
        # The most common source of that specific trap on a launch with
        # --no-sandbox already set is the zygote host: Chromium forks a
        # "zygote" helper process very early in startup (before the
        # single-process/multi-process decision is even made -- which is
        # why toggling --single-process had no effect), and that fork can
        # itself require sandbox/namespace setup (e.g. CLONE_NEWUSER) that
        # a locked-down PaaS container (Render) blocks at the kernel/runtime
        # level. --no-zygote skips that helper process entirely instead of
        # letting it attempt and trap. This is also why the long-"working"
        # atlas_url_capture.py was never actually proven safe by comparison
        # -- same zygote step, same risk, just not yet triggered there.
        _launch_args = ["--no-sandbox", "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage", "--disable-gpu",
                        "--no-zygote", "--single-process"]
        try:
            browser = await pw.chromium.launch(headless=True, args=_launch_args)
        except Exception as e:
            # Fallback: retry once with the system/apt-installed chromium
            # binary (CHROMIUM_PATH env, set in the Dockerfile) instead of
            # Playwright's own bundled chromium_headless_shell build -- in
            # case it's specifically that bundled revision that's broken/
            # incompatible in this container.
            import os
            sys_chromium = os.environ.get("CHROMIUM_PATH")
            logger.warning(
                f"[rayvila] bundled Chromium launch failed ({e}); "
                f"retrying with system chromium at {sys_chromium!r}"
            )
            if not sys_chromium:
                raise
            browser = await pw.chromium.launch(
                headless=True, executable_path=sys_chromium, args=_launch_args
            )
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            page = await context.new_page()
            # Block images/fonts/media (not needed for text/DOM scraping)
            # to reduce memory and bandwidth during the multi-page flow.
            try:
                await page.route(
                    re.compile(r"\.(png|jpe?g|gif|webp|svg|woff2?|ttf|mp4|mp3)(\?.*)?$", re.I),
                    lambda route: route.abort(),
                )
            except Exception as e:
                logger.debug(f"[rayvila] route-blocking setup skipped: {e}")
            try:
                await _goto_exam_page(page, url, candidate_name, progress_cb=progress_cb)
                html = await page.content()
            except Exception as e:
                logger.error(f"[rayvila] flow failed for {url}: {e}", exc_info=True)
                result["error"] = str(e)
                return result
        finally:
            await browser.close()

    _notify(progress_cb, "parsing")
    try:
        parsed_results = _parse_exam_html(html)
    except Exception as e:
        logger.error(f"[rayvila] HTML parse failed for {url}: {e}", exc_info=True)
        result["error"] = str(e)
        return result

    if not parsed_results:
        result["error"] = "exam.php থেকে কোনো প্রশ্ন পার্স করা যায়নি।"
        return result

    result["csv_bytes"] = results_to_csv_bytes(parsed_results)
    result["mcq_count"] = len(parsed_results)
    _notify(progress_cb, "done")
    return result
