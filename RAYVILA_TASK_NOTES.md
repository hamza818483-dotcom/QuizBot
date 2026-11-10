# Task: Rayvila auto-scraper (Playwright) — DONE (commits 864f1f3 + df95519)

## Goal
User gives a Rayvila exam link (e.g. `https://www.rayvila.com/g.php/240831004923`).
Bot should run the FULL flow headlessly (Playwright) and produce a CSV of
questions+options+answers automatically — same as the existing
`atlas_url_capture.py` link-capture feature, but site-specific with
scripted clicks instead of a single page snapshot.

## Exact flow (confirmed from 5 real .mhtml captures the user provided)

1. **login.php?tid=X** — fill `input#name` with candidate name (any
   placeholder text works, e.g. "Student"), click `div#login_browser`.
   Form posts to itself (`action="...login.php?tid=X"`).

2. **home.php?tid=X** — lands here after login. An ad banner loads async
   (Google ads iframe `#300x250-1`). **Wait ~10s** for it to settle (user's
   instruction — don't click through before the ad area finishes loading).
   Then click `a.res` with text "Start Test" (href goes to
   `confirm.php?tid=X&attempt=N` — attempt number increments per try, just
   follow the href/click, don't hardcode attempt=1).
   - User also mentioned: if an ad overlay/popup appears with its own close
     button, close it first. Not seen in the captures (the ad here was just
     an inline banner, not a blocking modal) — if a modal popup shows up in
     practice, look for a close "×" and click it before continuing. Handle
     defensively (try/except, don't fail the whole run if no popup appears).

3. **confirm.php?tid=X&attempt=N** — simple warning page. Click
   `a.res2` (text "Continue", href → `pre_test_preparation.php?tid=X&attempt=N`).

4. **pre_test_preparation.php** — NOT CAPTURED by user. Unknown whether
   it's a real interstitial page with its own button, or just auto-forwards
   immediately to `exam.php`. **Must handle both cases**: after navigating
   there, check if URL becomes `exam.php` on its own (wait for navigation /
   networkidle); if a button appears, find anything clickable (likely
   similarly classed `a.res`/`a.res2`/`.res3` or a form submit) and click it.
   Treat this step as unknown-needs-runtime-detection — this is the main
   risk area of the whole flow and should get a generous timeout + fallback
   screenshot/HTML dump on failure for debugging, not a hard-coded selector
   guess.

5. **exam.php?tid=X** — THE IMPORTANT PAGE. All questions + options +
   CORRECT ANSWERS are already in the DOM here, as hidden inputs, even
   before submitting:
   ```html
   <div id="1" class="tabcontent3">
     <p>  Q1.  <question text> </p>
     <div class="toolbar">
       <input type="radio" name="a1[]" value="1" id="1a" class="1a">
       <label for="1a"> A </label> <option A text> <p></p>
       ... (id="1b"/"1c"/"1d" for B/C/D, sometimes more)
     </div>
     <input type="text" name="r1" value="2" style="display:none;">  <!-- CORRECT ANSWER INDEX, 1-based -->
   </div>
   <div id="2" class="tabcontent3"> ... same pattern, r2, a2[] ... </div>
   ... up to however many questions (tq hidden field has the total count)
   ```
   Parsing recipe (pure HTML/regex or BeautifulSoup, NO AI needed, like
   Realm-of-Zoro / aapathshala parsers in atlas_mhtml.py):
   - For each `div.tabcontent3` with numeric `id="N"`:
     - question text = text inside its first `<p>` after stripping leading
       `Q{N}.` prefix
     - options = text immediately after each `<label for="{N}{letter}">
       {LETTER} </label>` up to the next `<p></p>` or next label/radio
     - correct answer = value of `<input name="r{N}" value="X">` (X is
       1-based: 1=A, 2=B, 3=C, 4=D)
   - Map to the existing CSV row schema used elsewhere in atlas_mhtml.py:
     `{"questions": ..., "option1": A, "option2": B, "option3": C,
       "option4": D, "option5": "", "answer": str(X), "explanation": "",
       "type": 1, "section": 1}`
   - This can be scraped via `page.content()` (just the rendered HTML) —
     no need to even click Submit. **This is the reliable, primary data
     source.**

6. **Submit** (optional, for cross-check / to match user's described flow
   exactly): scroll to bottom, click `a#sub2` (text "Submit") or the hidden
   `input#sub`. This POSTs to `action.php`, then redirects/shows result.
   Wait ~6s, then land on:

7. **view_result.php?tid=X** — human-readable confirmation of the same
   data (text-based, ads interspersed, NOT structured HTML — would need
   regex parsing):
   ```
   Q1.  <question text>
   A. <opt> B. <opt> C. <opt> D. <opt>
   Right Answer : D
   ```
   via regex: `r"Q(\d+)\.\s*(.+?)A\.\s*(.+?)B\.\s*(.+?)C\.\s*(.+?)D\.\s*(.+?)Right Answer\s*:\s*([A-D])"`
   (dotall mode, non-greedy). This is messier to parse than step 5's DOM
   and is NOT needed if step 5's scrape already works — keep this only as
   an optional secondary verification path, not the primary one. Don't
   over-invest here if time/budget is short; step 5 alone fully solves the
   task.

## Captured reference files (for exact HTML structure — ask user to
## re-share in the new chat if needed, they are NOT committed to this repo
## since they contain a real candidate's test session data):
- Login page HTML, Home/Start-Test page HTML, Confirm page HTML,
  Examination page HTML (the money page — step 5 above), View_Results HTML
  — all already inspected; exact markup is reproduced inline in this doc
  above (steps 1,2,3,5,7), so re-fetching the files is NOT required to
  implement this — the note above has everything needed.

## Implementation plan
- New file: `atlas_rayvila.py` in this repo (hamza818483-dotcom/QuizBot),
  sibling to `atlas_url_capture.py`. Playwright async, same
  launch-args/pattern as `atlas_url_capture.py::capture_url_as_mhtml`.
  Function: `async def scrape_rayvila_exam(url: str, candidate_name: str =
  "Student") -> dict` returning the same shape
  `atlas_url_capture.process_url_to_mhtml_and_csv` returns:
  `{"csv_bytes": bytes, "mcq_count": int, "error": str|None}`.
- Detect Rayvila URLs: `tid` in query string or path matches
  `rayvila.com` domain and `/g.php/` or `/exam/` path — add a
  `is_rayvila_url()` helper so the generic `/api/url-to-csv` endpoint (see
  below) can route to this specific flow instead of the generic
  screenshot-and-guess approach.
- Reuse `atlas_mhtml.py::results_to_csv_bytes()` for the final CSV
  (don't reinvent).
- Wire into `app.py`'s existing `/api/url-to-csv` endpoint (already
  pushed, commit 8971c58 + 8971c58's JSON-body fix) — IMPORTANT: that
  endpoint currently ALWAYS calls `atlas_url_capture.process_url_to_mhtml_and_csv`
  (generic screenshot+parse). Needs a branch: if `is_rayvila_url(url)`,
  call `atlas_rayvila.scrape_rayvila_exam(url)` instead.
  - NOTE: per the user's most recent instruction (this same chat, just
    before this task), the LMS ExamForm UI itself does NOT want a
    separate "paste URL" input box — that was reverted
    (lms-1 commit e7088aa). So this Rayvila feature's primary UI entry
    point should be the **Telegram bot** (DM: user sends the rayvila link
    as a plain message, same as the existing generic link-capture
    feature in `atlas_url_capture.py`/`app.py`'s `handle_message`) rather
    than through the LMS web UI. Confirm with user if unsure, but default
    to Telegram-first since that's the established pattern for this exact
    kind of feature in this codebase.
  - Find where `atlas_url_capture.is_plain_url()` is used inside
    `handle_message` (search app.py for `is_plain_url`) and add a branch
    there too: if `is_rayvila_url(text)`, call the new scraper instead of
    the generic capture, and send back the resulting CSV file in DM
    (same send-file pattern already used for the generic link-capture
    result).

## State as of this note
- DONE: `atlas_rayvila.py` implemented (commit `864f1f3`) — `is_rayvila_url()`,
  `scrape_rayvila_exam()` with full login→home→confirm→pre_test_preparation→exam
  click-through, DOM parsing of exam.php via BeautifulSoup, CSV via
  `atlas_mhtml.results_to_csv_bytes()`.
- DONE: `app.py` wired (commit `df95519`):
  - `/api/url-to-csv` branches to `scrape_rayvila_exam()` when `is_rayvila_url(url)`.
  - Telegram DM `handle_message` checks `is_rayvila_url(text)` before the
    generic `is_plain_url(text)` branch, routing to `_process_rayvila_auto()`
    which sends the resulting CSV back in DM.
- NOT implemented (per spec, intentionally out of scope): the optional
  Submit → view_result.php secondary verification path (step 6/7) — step 5's
  DOM scrape is the primary/sufficient data source per the task notes.
- Submit click (`a#sub2`/`input#sub`) was also left out since it's optional
  and not needed for the CSV output; only the exam.php DOM is scraped.
- Not yet tested against a live Rayvila exam link (no live URL/session was
  available during this task) — implementation follows the exact markup
  reproduced from the 5 real .mhtml captures in this doc. If a real run
  surfaces selector mismatches (especially on pre_test_preparation.php,
  flagged as the main risk area), re-open this file and adjust.

## Follow-up: live DM dashboard (commits 045c613 + 856cd63)
- `atlas_rayvila.py`: `scrape_rayvila_exam()` and `_goto_exam_page()` now take
  `progress_cb(step, pct)`, called at each flow stage (opening/login/home/
  waiting_ad/confirm/pre_test_preparation/exam/parsing/done).
- `app.py`: `_process_rayvila_auto()` now spawns `_rayvila_live_updater()`
  (same pattern as `_mhtml_live_updater`/`MHTML_JOBS`, own `RAYVILA_JOBS`
  dict) which edits the Telegram DM loading message every ~1s with:
  current step label (Bengali), progress bar + %, elapsed time since start.
  No "premium%" concept beyond plain progress % — user confirmed that's
  what they meant when asked.
