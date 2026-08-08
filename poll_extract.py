# ============================================================
# ATLAS BOT — Poll Extractor (poll_extract.py)
# /poll <link1> \n <link2>
# Telethon দিয়ে channel থেকে directly poll extract করে:
#   1. CSV file send করে
#   2. D1 তে quiz save করে permanent link দেয়
# No forward needed. Fully independent from app.py logic.
# ============================================================

import os
import re
import csv
import json
import asyncio
import logging
import time
from io import StringIO
from telethon.errors import FloodWaitError

logger = logging.getLogger("atlas.poll_extract")

API_ID       = int(os.environ.get("API_ID", "33312774"))
API_HASH     = os.environ.get("API_HASH", "883db3366f8759d1d14c861c0d628232")
SESSION_STR  = os.environ.get("SESSION_STRING", "")

# URL matcher — http(s) links and bare t.me/telegram.me links
_URL_RE = re.compile(
    r'(?:https?://\S+|(?:t\.me|telegram\.me)/\S+)',
    re.IGNORECASE
)

def _clean_extracted_text(text: str) -> str:
    """Applied to question, every option, and explanation of extracted polls:
    - 'sabas'/'SABAS'/'Sabas' (any case, with or without brackets) → 'ATLAS'
    - any link/URL → '✅Join:@MediAtlas'
    """
    if not text:
        return text
    t = str(text)
    t = re.sub(r'\[sabas\]', '[ATLAS]', t, flags=re.IGNORECASE)
    t = re.sub(r'\bsabas\b', 'ATLAS', t, flags=re.IGNORECASE)
    t = _URL_RE.sub('✅Join:@MediAtlas', t)
    return t


# ── Link parser ──────────────────────────────────────────────
def parse_tg_link(link: str):
    """
    Returns (channel_entity, msg_id, topic_id)
    Private:       t.me/c/123/456       → (int(-100123), 456, None)
    Private topic: t.me/c/123/3/456     → (int(-100123), 456, 3)
    Public:        t.me/mychan/456       → ("mychan", 456, None)
    Public topic:  t.me/mychan/3/456    → ("mychan", 456, 3)
    """
    link = link.strip().rstrip("/")
    # Private topic: t.me/c/{chat}/{topic}/{msg}
    m = re.search(r"t\.me/c/(\d+)/(\d+)/(\d+)", link)
    if m:
        return int(f"-100{m.group(1)}"), int(m.group(3)), int(m.group(2))
    # Private: t.me/c/{chat}/{msg}
    m = re.search(r"t\.me/c/(\d+)/(\d+)", link)
    if m:
        return int(f"-100{m.group(1)}"), int(m.group(2)), None
    # Public topic: t.me/{username}/{topic}/{msg}
    m = re.search(r"t\.me/([A-Za-z0-9_]+)/(\d+)/(\d+)", link)
    if m:
        return m.group(1), int(m.group(3)), int(m.group(2))
    # Public: t.me/{username}/{msg}
    m = re.search(r"t\.me/([A-Za-z0-9_]+)/(\d+)", link)
    if m:
        return m.group(1), int(m.group(2)), None
    return None, None, None


# ── Telethon extract ─────────────────────────────────────────
def _format_elapsed(seconds: float) -> str:
    """Second-ke human-readable format e convert kore: '2m 34s' ba '1h 5m 12s'"""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    elif m > 0:
        return f"{m}m {s}s"
    else:
        return f"{s}s"


class _AdaptiveRateLimiter:
    """Proactive + reactive rate limiting:
    1. Sliding-window request counter — proti 60s e max N-ta SendVoteRequest
       call korte dey, limit-er kach e gele nijei slow hoye jay (FloodWait
       howar AGE-i, reactive na hoye proactive).
    2. FloodWait actually hit hole (safety-net hishebe) delay aro barায়,
       r safe-per-minute limit-o kome jay — pura process-er baki shomoy-er
       jonno aro conservative hoye jay।
    """
    def __init__(self):
        self.current_delay = 0.8       # base delay per vote-request, seconds
        self.flood_hits = 0
        self.max_per_minute = 20       # conservative safe limit — Telegram
                                        # official client-o emon range-e thake
        self.request_times = []        # sliding window er timestamps

    def register_flood_wait(self, wait_seconds: float):
        self.flood_hits += 1
        # FloodWait hit mane amader estimate bhul chilo — aro strict hote hobe
        self.current_delay = min(self.current_delay * 2.5, 15.0)
        self.max_per_minute = max(5, int(self.max_per_minute * 0.5))
        logger.warning(
            f"[poll_extract] FloodWait #{self.flood_hits} ({wait_seconds}s) — "
            f"delay {self.current_delay:.1f}s, max/min {self.max_per_minute} e barano holo"
        )

    async def wait_before_request(self):
        """Proactive check — proti request-er AGE call koro. Sliding window-e
        max_per_minute cross korle nijei extra wait kore, FloodWait howar
        age-i slow hoye jay."""
        now = time.monotonic()
        # 60s-er beshi purono timestamp shorie dao
        self.request_times = [t for t in self.request_times if now - t < 60]

        if len(self.request_times) >= self.max_per_minute:
            # Window-er shobcheye purono request 60s hoye jawa porjonto wait koro
            oldest = self.request_times[0]
            wait_needed = 60 - (now - oldest) + 0.5
            if wait_needed > 0:
                logger.info(f"[poll_extract] proactive slowdown — {wait_needed:.1f}s wait ({len(self.request_times)}/{self.max_per_minute} per-min limit near)")
                await asyncio.sleep(wait_needed)

        # Base delay-o always maintain koro (per-request minimum gap)
        await asyncio.sleep(self.current_delay)
        self.request_times.append(time.monotonic())

    def get_delay(self) -> float:
        return self.current_delay


_rate_limiter = _AdaptiveRateLimiter()


def _get_process_memory_mb():
    """Current process-er RSS memory usage MB te return kore.
    psutil na thakle ba error hole None return kore (checkpoint trigger skip hoy)."""
    try:
        import psutil
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / (1024 * 1024)
    except Exception:
        return None


class PollList(list):
    """Plain list e attribute set kora jay na (AttributeError) — tai
    ei subclass use kore, jate polls.skipped_ids reliably kaj kore."""
    skipped_ids: list = []


async def extract_polls_telethon(channel, start_id: int, end_id: int, progress_cb=None, topic_id=None, checkpoint_cb=None) -> list:
    """
    Telethon দিয়ে channel থেকে start_id→end_id range এর
    সব quiz poll extract করে list of dict return করে।
    Batch of 15 poll ekbare guaranteed vote+confirm kore process kore —
    kono poll silently skip hoy na, na parle "MANUALLY VERIFY" mark kore rakhe.
    progress_cb(checked, found, elapsed) — optional callback every poll
    checkpoint_cb(polls_so_far, is_final) — optional callback every N polls,
    boro range e crash/timeout hole ওই porjonto CSV পাঠানোর জন্য।
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl import functions

    polls = PollList()
    manual_review_ids = []
    client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
    await client.connect()

    try:
        try:
            entity = await client.get_entity(channel)
        except (ValueError, TypeError) as e:
            logger.warning(f"[poll_extract] entity not cached, refreshing dialogs: {e}")
            await client.get_dialogs(limit=200)
            try:
                entity = await client.get_entity(channel)
            except Exception:
                raise Exception(
                    "এই channel/group এর entity resolve করা যায়নি — "
                    "Session account-টা কি এই channel-এ join করা আছে? "
                    "না থাকলে join করিয়ে আবার try করো।"
                )

        # ── Step 1: শুধু quiz poll message গুলো collect করো (fast pass, no vote) ──
        quiz_messages = []
        checked = 0
        collect_attempts = 0
        scan_from_id = start_id - 1  # ei id-er porer message theke scan shuru hobe
        while True:
            try:
                async for message in client.iter_messages(
                    entity,
                    min_id=scan_from_id,
                    max_id=end_id + 1,
                    limit=end_id - scan_from_id,
                    reverse=True,
                ):
                    checked += 1
                    scan_from_id = message.id  # last-seen track koro, resume korার jonno

                    if topic_id and message.reply_to:
                        msg_topic = getattr(message.reply_to, "reply_to_top_id", None) or getattr(message.reply_to, "reply_to_msg_id", None)
                        if msg_topic != topic_id:
                            if progress_cb:
                                await progress_cb(checked, len(polls))
                            continue
                    elif topic_id and not message.reply_to:
                        if progress_cb:
                            await progress_cb(checked, len(polls))
                        continue

                    if not message.poll or not getattr(message.poll.poll, "quiz", False):
                        if progress_cb:
                            await progress_cb(checked, len(polls))
                        continue

                    quiz_messages.append(message)
                break  # scan সম্পূর্ণ হলে loop থেকে বের হও
            except FloodWaitError as fw:
                logger.warning(f"[poll_extract] Step1 scan FloodWait {fw.seconds}s — wait kore oi jaygay theke resume korbo (scan_from_id={scan_from_id})")
                _rate_limiter.register_flood_wait(fw.seconds)
                await asyncio.sleep(fw.seconds + 1)
                # quiz_messages/checked reset kori NA — jekhane chilo shekhan thekei continue
            except Exception as e:
                collect_attempts += 1
                logger.warning(f"[poll_extract] message scan error (attempt {collect_attempts}): {type(e).__name__}: {e} — {scan_from_id} theke resume korbo")
                await asyncio.sleep(2.0 * collect_attempts)
                if collect_attempts >= 10:
                    raise Exception(f"Message scan {collect_attempts} bar fail holo, network/API issue check koro: {e}")

        # ── Step 2: batch of 15 kore guaranteed vote+confirm process ──
        import time as _time
        extract_start = _time.monotonic()
        BATCH_SIZE = 15
        MEMORY_LIMIT_MB = 10000  # 16GB RAM er beshirbhag, ei level cross korle checkpoint CSV pathabe (safety net)
        checkpoint_sent_for_level = 0  # kotobar checkpoint hoyeche track kore, duplicate na hoy

        _last_msg_id_seen = None
        try:
            for batch_start in range(0, len(quiz_messages), BATCH_SIZE):
                batch = quiz_messages[batch_start:batch_start + BATCH_SIZE]
                for message in batch:
                    _last_msg_id_seen = message.id
                    entry, resolved = await _process_single_poll(client, channel, message)
                    if resolved:
                        polls.append(entry)
                    else:
                        manual_review_ids.append(message.id)
                        polls.append(entry)  # rakhbo, kintu MANUALLY VERIFY mark shoho

                    if progress_cb:
                        elapsed = _time.monotonic() - extract_start
                        await progress_cb(checked, len(polls), elapsed)
                    await asyncio.sleep(_rate_limiter.get_delay())

                    if checkpoint_cb:
                        mem_mb = _get_process_memory_mb()
                        if mem_mb and mem_mb >= MEMORY_LIMIT_MB * (checkpoint_sent_for_level + 1):
                            checkpoint_sent_for_level += 1
                            logger.warning(f"[poll_extract] memory {mem_mb:.0f}MB cross korlo — checkpoint CSV pathano hocche ({len(polls)} polls)")
                            try:
                                await checkpoint_cb(list(polls), False)
                            except Exception as cb_err:
                                logger.warning(f"[poll_extract] checkpoint_cb error: {cb_err}")

                # Batch er por ektu beshi break — rate-limit/timing safety
                if batch_start + BATCH_SIZE < len(quiz_messages):
                    await asyncio.sleep(max(2.0, _rate_limiter.get_delay() * 3))
        except Exception as e:
            # Bipod hole (crash/timeout) — jotukhon collect hoyeche oi porjonto
            # CSV pathiye dao, pura kaj shesh na hoyeo kichu na kichu hate thake
            logger.error(f"[poll_extract] batch loop crashed mid-way at {len(polls)} polls: {type(e).__name__}: {e}")
            if checkpoint_cb and polls:
                try:
                    await checkpoint_cb(list(polls), True)
                except Exception:
                    pass
            # Last successfully-attempted message id attach kore rakhi, jate
            # caller auto-resume korte pare oi jaygay theke
            e.last_message_id = _last_msg_id_seen
            e.partial_polls = list(polls)
            raise

    finally:
        await client.disconnect()

    polls.skipped_ids = manual_review_ids  # ekhon r silently skip hoy na, "manual review" list
    return polls


async def _process_single_poll(client, channel, message):
    """Ekta single quiz poll ke bot nijei guaranteed vote diye confirm kora
    porjonto retry kore — infinite retry, kono time cap nai. Poll ba
    message-i deleted hoye gele shudhu tokhon best-effort fallback (karon
    tokhon actual data-i r exist kore na, kono kichu extract kora impossible)।
    Baki shob normal case e bot vote na dewa r result na paowa porjonto
    lege thakbe।"""
    from telethon.tl import functions

    p = message.poll.poll
    q_text = p.question.text if hasattr(p.question, "text") else str(p.question)
    q_text = _clean_extracted_text(q_text)

    options = []
    for ans in p.answers:
        opt = ans.text.text if hasattr(ans.text, "text") else str(ans.text)
        options.append(_clean_extracted_text(opt))

    def _parse_results(res):
        cidx, expl = 0, ""
        found = False
        if res and getattr(res, "results", None):
            for i, r in enumerate(res.results):
                if getattr(r, "correct", False):
                    cidx = i
                    found = True
                    break
        if res and getattr(res, "solution", None):
            expl = res.solution
        return cidx, expl, found

    correct_idx, explanation, found = 0, "", False
    try:
        correct_idx, explanation, found = _parse_results(message.poll.results)
    except Exception:
        pass

    max_wait = 6.0
    attempt = 0

    while not found:
        attempt += 1

        # Strategy 1: vote diye direct result check
        await _rate_limiter.wait_before_request()
        try:
            vote_res = await client(functions.messages.SendVoteRequest(
                peer=channel,
                msg_id=message.id,
                options=[p.answers[0].option]
            ))
            vote_poll_results = _extract_poll_results_from_updates(vote_res)
            if vote_poll_results:
                correct_idx, explanation, found = _parse_results(vote_poll_results)
        except FloodWaitError as fw:
            logger.warning(f"[poll_extract] msg {message.id}: FloodWait {fw.seconds}s — Telegram er kotha moto wait kortesi")
            _rate_limiter.register_flood_wait(fw.seconds)
            await asyncio.sleep(fw.seconds + 1)
        except Exception:
            pass  # Already voted — ok, fallback to refetch below

        if found:
            break

        # Strategy 2: message refetch kore poll.results check
        wait = min(1.0 + attempt * 0.5, max_wait)
        await asyncio.sleep(wait)
        await _rate_limiter.wait_before_request()
        try:
            fetched = await client.get_messages(channel, ids=message.id)
            if not fetched:
                # Message-i r exist kore na — data literally gone, extract impossible
                logger.warning(f"[poll_extract] msg {message.id}: message delete hoye gese, actual data nai — best-effort fallback")
                break
            if not fetched.poll:
                logger.warning(f"[poll_extract] msg {message.id}: ar poll na (edited/removed) — best-effort fallback")
                break
            correct_idx, explanation, found = _parse_results(fetched.poll.results)
        except Exception:
            pass

        if found:
            break

        # Strategy 3 (every 5th attempt): entity/session re-resolve — kokhono
        # kokhono connection-level caching issue er jonno result na ashte pare
        if attempt % 5 == 0:
            await _rate_limiter.wait_before_request()
            try:
                fresh_entity = await client.get_entity(channel)
                fetched = await client.get_messages(fresh_entity, ids=message.id)
                if fetched and fetched.poll:
                    correct_idx, explanation, found = _parse_results(fetched.poll.results)
            except Exception:
                pass
            logger.info(f"[poll_extract] msg {message.id}: still retrying, attempt {attempt}")

    explanation = _clean_extracted_text(explanation)

    if len(options) > 4:
        if found and correct_idx >= 4:
            options = options[:3] + [options[correct_idx]]
            correct_idx = 3
        else:
            options = options[:4]

    return {
        "question": q_text,
        "options": options,
        "correct_idx": correct_idx,
        "answer": correct_idx + 1,
        "explanation": explanation,
    }, found




# ── CSV builder ──────────────────────────────────────────────
def _extract_poll_results_from_updates(vote_res):
    """SendVoteRequest returns an Updates object, NOT a Poll directly.
    The actual poll results live inside vote_res.updates as an
    UpdateMessagePoll entry. This pulls it out."""
    if not vote_res:
        return None
    updates_list = getattr(vote_res, "updates", None) or []
    for upd in updates_list:
        if hasattr(upd, "results") and hasattr(upd, "poll_id"):
            return upd.results
    return None


def _skipped_note(polls) -> str:
    """polls.skipped_ids থাকলে caption এ যোগ করার মতো note বানায়।
    এই list এখন শুধু extreme edge-case (90s timeout, poll deleted) এর
    জন্য populate হয় — normal active poll এ কখনো ঘটবে না।"""
    skipped = getattr(polls, "skipped_ids", None)
    if not skipped:
        return ""
    ids_str = ", ".join(str(i) for i in skipped[:15])
    more = f" (+{len(skipped)-15} more)" if len(skipped) > 15 else ""
    return (
        f"\n\n⚠️ <b>{len(skipped)} টা poll এ answer confirm করতে সমস্যা হয়েছে</b> "
        f"(poll closed/deleted হয়ে থাকতে পারে):\n"
        f"IDs: {ids_str}{more}\n"
        f"এগুলো CSV-তে best-effort options soho আছে, একবার নিজে চেক করো।"
    )


def build_csv(polls: list) -> bytes:
    """
    polls list → CSV bytes (utf-8-sig for Excel Bengali support)
    Columns: questions,option1,option2,option3,option4,option5,answer,explanation,type,section
    """
    output = StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow([
        "questions",
        "option1", "option2", "option3", "option4", "option5",
        "answer", "explanation", "type", "section"
    ])
    for p in polls:
        padded = (p["options"] + ["", "", "", "", ""])[:5]
        writer.writerow([
            p["question"],
            padded[0], padded[1], padded[2], padded[3], padded[4],
            p["answer"],        # 1-based numeric
            p["explanation"],
            1,                  # type  — fixed
            1,                  # section — fixed
        ])
    return output.getvalue().encode("utf-8-sig")


# ── D1 quiz save + Supabase backup ──────────────────────────
async def save_quiz_to_d1(polls: list, name: str, uid: int) -> str | None:
    """
    polls list → D1 quizzes table এ save + Supabase quiz_backups এ backup।
    quiz.py এর format: {question, options, answer_index (0-based int), explanation}
    """
    from core import d1_run
    from pdf_handler import gen_session_id

    questions = []
    for p in polls:
        questions.append({
            "question":    p["question"],
            "options":     p["options"],
            "answer_index": p["correct_idx"],
            "explanation": p["explanation"],
        })

    quiz_id = "qz_" + gen_session_id()[:8]

    # ── D1 save ──
    d1_ok = False
    try:
        await d1_run(
            "INSERT OR REPLACE INTO quizzes "
            "(id, name, description, timer, shuffle, csv_data, tag, exp_footer, created_by) "
            "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            [
                quiz_id, name,
                f"Special Topic — {len(questions)} প্রশ্ন",
                30, 0,
                json.dumps(questions),
                "", "", uid,
            ]
        )
        d1_ok = True
    except Exception as e:
        logger.error(f"[poll_extract] D1 save error: {e}")

    # ── Supabase backup (Primary + Secondary dual-write) ──
    payload = {
        "quiz_id": quiz_id,
        "name": name,
        "questions": questions,
        "created_by": uid,
    }
    import httpx

    # Primary Supabase
    try:
        from core import SUPABASE_URL, SUPABASE_KEY
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{SUPABASE_URL}/rest/v1/quiz_backups", headers=headers, json=payload)
        logger.info(f"[poll_extract] Supabase Primary backup ok: {quiz_id}")
    except Exception as e:
        logger.warning(f"[poll_extract] Supabase Primary backup failed: {e}")

    # Secondary Supabase mirror removed — project paused/deleted (DNS fail on every call)

    return quiz_id if (d1_ok) else None


# ── Main handler ─────────────────────────────────────────────
async def handle_poll_extract(msg: dict):
    """
    /poll
    https://t.me/c/.../101
    https://t.me/c/.../250

    Extracts all quiz polls in range → sends CSV + permanent quiz link.
    """
    from core import send_msg, edit_msg, send_document, tg_post
    import time as _overall_time
    overall_start = _overall_time.monotonic()

    chat_id = msg["chat"]["id"]
    uid     = msg["from"]["id"]
    text    = msg.get("text", "").strip()

    # Parse links from message body (newline separated)
    body  = re.sub(r"^/poll\s*", "", text, flags=re.IGNORECASE).strip()
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    links = [l for l in lines if "t.me/" in l]

    if len(links) < 2:
        await send_msg(chat_id,
            "❌ দুটো link দাও!\n\n"
            "📌 Format:\n"
            "<code>/poll\n"
            "https://t.me/c/.../101\n"
            "https://t.me/c/.../250</code>\n\n"
            "• প্রথম link = range start\n"
            "• দ্বিতীয় link = range end",
            parse_mode="HTML"
        )
        return

    ch1, start_id, topic1 = parse_tg_link(links[0])
    ch2, end_id,   topic2 = parse_tg_link(links[1])

    if not ch1 or not start_id or not end_id:
        await send_msg(chat_id, "❌ Link parse হয়নি। সঠিক Telegram link দাও।")
        return

    if ch1 != ch2:
        await send_msg(chat_id, "❌ দুটো link একই channel/group এর হতে হবে!")
        return

    topic_id = topic1 or topic2  # topic filter

    if start_id > end_id:
        start_id, end_id = end_id, start_id

    total = end_id - start_id + 1
    if total > 3000:
        logger.info(f"[poll_extract] Large range requested: {total} messages ({start_id}-{end_id}) — proceeding without cap")

    if not SESSION_STR:
        await send_msg(chat_id, "❌ SESSION_STRING set নেই। HF Space secrets এ add করো।")
        return

    # Status message
    r = await send_msg(chat_id,
        f"⏳ Scan করছি: {start_id} → {end_id} ({total} messages)"
        + (f" [Topic: {topic_id}]" if topic_id else "") + "...",
        parse_mode="HTML"
    )
    status_id = r.get("result", {}).get("message_id")

    # Progress callback
    import time as _time
    _last_edit = {"t": 0.0}

    async def progress(checked, found, elapsed=None):
        if not status_id:
            return
        now = _time.monotonic()
        # Throttle edits to once every ~2s to avoid Telegram rate-limit,
        # but always allow first/last update
        if now - _last_edit["t"] < 2.0 and checked < total:
            return
        _last_edit["t"] = now
        time_str = f"⏱️ সময়: {_format_elapsed(elapsed)}" if elapsed is not None else ""
        lines = [
            f"⏳ চেক: {checked}/{total}",
            f"📋 Poll পেয়েছি: {found}",
        ]
        if time_str:
            lines.append(time_str)
        if _rate_limiter.flood_hits > 0:
            lines.append(f"🐢 FloodWait {_rate_limiter.flood_hits}x — delay {_rate_limiter.current_delay:.1f}s")
        await edit_msg(chat_id, status_id,
            "\n".join(lines),
            parse_mode="HTML"
        )

    # Checkpoint callback — boro range e crash/timeout hole ওই porjonto CSV pathay
    async def checkpoint(polls_so_far, is_final):
        if not polls_so_far:
            return
        try:
            interim_csv = build_csv(polls_so_far)
            interim_name = f"CHECKPOINT_polls_{ch1_str_holder['v']}_{start_id}_{end_id}_upto{len(polls_so_far)}.csv"
            label = "🛑 বিপদ! এই পর্যন্ত যা পেয়েছি (process থেমে গেছে):" if is_final else "💾 Checkpoint — এখন পর্যন্ত সংগৃহীত:"
            await send_document(
                chat_id, interim_csv, interim_name,
                caption=f"{label}\n📋 Poll: <b>{len(polls_so_far)}</b>\n📌 Range: {start_id} → {end_id}",
                mime_type="text/csv"
            )
        except Exception as e:
            logger.error(f"[poll_extract] checkpoint send failed: {e}")

    ch1_str_holder = {"v": str(ch1).lstrip("@").replace("-100", "")}

    # Extract — crash hole automatically bakita continue kore (max 5 bar resume try)
    all_polls = PollList()
    all_polls.skipped_ids = []
    resume_start = start_id
    resume_attempts = 0
    MAX_RESUME_ATTEMPTS = 5

    while True:
        try:
            batch_polls = await extract_polls_telethon(ch1, resume_start, end_id, progress_cb=progress, topic_id=topic_id, checkpoint_cb=checkpoint)
            all_polls.extend(batch_polls)
            all_polls.skipped_ids.extend(getattr(batch_polls, "skipped_ids", []))
            break  # shob shesh, r kono crash hoyni
        except Exception as e:
            partial = getattr(e, "partial_polls", None)
            last_id = getattr(e, "last_message_id", None)
            if partial:
                all_polls.extend(partial)

            resume_attempts += 1
            if last_id and resume_attempts <= MAX_RESUME_ATTEMPTS:
                logger.warning(f"[poll_extract] crash hoyeche, msg {last_id} theke auto-resume korchi (attempt {resume_attempts})")
                await send_msg(chat_id, f"⚠️ সমস্যা হয়েছিল, কিন্তু auto-resume করছি msg {last_id} থেকে... (attempt {resume_attempts}/{MAX_RESUME_ATTEMPTS})")
                resume_start = last_id + 1
                await asyncio.sleep(3.0)
                continue
            else:
                logger.error(f"[poll_extract] Telethon error, resume o fail: {e}")
                if all_polls:
                    csv_bytes = build_csv(all_polls)
                    ch_str = str(ch1).lstrip("@").replace("-100", "")
                    filename = f"PARTIAL_polls_{ch_str}_{start_id}_{end_id}.csv"
                    await send_document(
                        chat_id, csv_bytes, filename,
                        caption=f"⚠️ Auto-resume {resume_attempts} bar try kore o pura shesh hoyni।\n📋 Ei porjonto pawa poll: <b>{len(all_polls)}</b>\n\nবাকি অংশের জন্য নতুন range দিয়ে আবার /poll চালাও (শুরু: msg {resume_start})",
                        parse_mode="HTML",
                        mime_type="text/csv"
                    )
                else:
                    await send_msg(chat_id, f"❌ Error: {e}")
                return

    polls = all_polls

    if not polls:
        await send_msg(chat_id,
            f"😕 এই range এ কোনো quiz poll পাওয়া যায়নি।\n({total} messages চেক হয়েছে)"
        )
        return

    # Build CSV
    csv_bytes = build_csv(polls)
    ch_str    = str(ch1).lstrip("@").replace("-100", "")
    filename  = f"polls_{ch_str}_{start_id}_{end_id}.csv"

    # Save to D1 → get permanent quiz link (default topic always "Special Topic")
    quiz_name = "Special Topic"
    quiz_id   = await save_quiz_to_d1(polls, quiz_name, uid)

    bot_info     = await tg_post("getMe", {})
    bot_username = bot_info.get("result", {}).get("username", "atlasQuizProBot")

    # একটাই smart link — ভেতরে HF→CF→Supabase auto fallback
    web_link = f"https://hamza818483-dotcom.github.io/QuizBot/exam.html?id={quiz_id}" if quiz_id else None
    bot_link = f"https://t.me/{bot_username}?start={quiz_id}" if quiz_id else None

    total_elapsed = _overall_time.monotonic() - overall_start
    time_display = _format_elapsed(total_elapsed)

    caption = (
        f"✅ <b>Poll Extract সম্পন্ন!</b>\n"
        f"📌 Range: {start_id} → {end_id}\n"
        f"📋 Poll পেয়েছি: <b>{len(polls)}</b>\n"
        f"⏱️ মোট সময়: <b>{time_display}</b>\n\n"
    )
    if web_link:
        caption += f"🌐 <b>Web Quiz:</b>\n{web_link}\n\n"
    if bot_link:
        caption += f"🤖 <b>Bot Quiz:</b>\n{bot_link}"
    caption += _skipped_note(polls)

    doc_result = await send_document(
        chat_id, csv_bytes, filename,
        caption=caption,
        mime_type="text/csv"
    )

    # Auto-pin the response message
    try:
        sent_msg_id = doc_result.get("result", {}).get("message_id") if doc_result else None
        if sent_msg_id:
            await tg_post("pinChatMessage", {
                "chat_id": chat_id,
                "message_id": sent_msg_id,
                "disable_notification": True
            })
    except Exception as e:
        logger.warning(f"[poll_extract] Auto-pin failed: {e}")


# ── Batch-grouped scan (for /ok summary) ─────────────────────
async def scan_poll_batches_telethon(channel, start_id: int, end_id: int,
                                      progress_cb=None, topic_id=None) -> list:
    """
    Range এর সব message স্ক্যান করে quiz poll গুলোকে 'batch' এ ভাগ করে।
    Consecutive quiz poll messages একই batch; মাঝে non-poll message (pre-msg/
    end-msg/gap) পড়লে নতুন batch শুরু হয়।
    Returns: [(first_poll_msg_id, poll_count_in_batch), ...]
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    batches = []
    current_first_id = None
    current_count = 0

    client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
    await client.connect()
    try:
        try:
            entity = await client.get_entity(channel)
        except (ValueError, TypeError):
            await client.get_dialogs(limit=200)
            entity = await client.get_entity(channel)

        checked = 0
        async for message in client.iter_messages(
            entity,
            min_id=start_id - 1,
            max_id=end_id + 1,
            limit=end_id - start_id + 1,
            reverse=True,
        ):
            checked += 1

            if topic_id and message.reply_to:
                msg_topic = getattr(message.reply_to, "reply_to_top_id", None) or getattr(message.reply_to, "reply_to_msg_id", None)
                if msg_topic != topic_id:
                    continue
            elif topic_id and not message.reply_to:
                continue

            is_quiz_poll = bool(message.poll) and getattr(message.poll.poll, "quiz", False)

            if is_quiz_poll:
                if current_first_id is None:
                    current_first_id = message.id
                    current_count = 1
                else:
                    current_count += 1
            else:
                if current_first_id is not None:
                    batches.append((current_first_id, current_count))
                    current_first_id = None
                    current_count = 0

            if progress_cb and checked % 100 == 0:
                await progress_cb(checked, len(batches))

        if current_first_id is not None:
            batches.append((current_first_id, current_count))
    finally:
        await client.disconnect()

    return batches


def build_batch_link(channel, msg_id: int, topic_id=None) -> str:
    """channel + msg_id → t.me link (public username বা private /c/ format)"""
    if isinstance(channel, str):
        base = f"https://t.me/{channel}"
    else:
        ch_str = str(channel).replace("-100", "")
        base = f"https://t.me/c/{ch_str}"
    if topic_id:
        return f"{base}/{topic_id}/{msg_id}"
    return f"{base}/{msg_id}"


def build_ok_summary(total_polls: int, batches_with_links: list) -> str:
    """
    batches_with_links = [(part_num, link, count), ...]
    csv_get_master_summary এর same style এ summary বানায়।
    """
    text = (
        f"🌟মোট প্রশ্ন: {total_polls}\n"
        f"📦 মোট ব্যাচ: {len(batches_with_links)}\n\n"
    )
    for part_n, link, count in batches_with_links:
        text += f"📍Part-{part_n:02d}: ({count}টি প্রশ্ন)\n{link}\n\n"
    text += (
        "📌 *এটলাসের Exam Batch* এ অসংখ্য প্রশ্ন প্রাক্টিসের সুযোগ আছে।\n"
        "💬 *Whatsapp:* wa.me/8801999681290\n"
        "🌟 *Website:* Atlascourses.com"
    )
    return text


# ── Forum topic listing ──────────────────────────────────────
async def get_forum_topics_ordered(channel, limit=200) -> list:
    """
    Group এ এই মুহূর্তে topic list এ যেভাবে দেখায় (pinned topics আগে,
    তারপর সাম্প্রতিক activity অনুযায়ী) ঠিক সেই order এ topics return করে —
    এটাই Telegram এর GetForumTopicsRequest এর নিজস্ব default sort, তাই
    কোনো re-sort করা হয় না।
    Returns: [(topic_id, topic_title), ...]
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.functions.messages import GetForumTopicsRequest

    client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
    await client.connect()
    try:
        try:
            entity = await client.get_entity(channel)
        except (ValueError, TypeError):
            await client.get_dialogs(limit=200)
            entity = await client.get_entity(channel)

        all_topics = []
        offset_date = None
        offset_id = 0
        offset_topic = 0
        while True:
            result = await client(GetForumTopicsRequest(
                peer=entity,
                offset_date=offset_date,
                offset_id=offset_id,
                offset_topic=offset_topic,
                limit=min(100, limit - len(all_topics)),
            ))
            if not result.topics:
                break
            for t in result.topics:
                tid = getattr(t, "id", None)
                title = getattr(t, "title", None)
                if tid is None or title is None:
                    continue
                all_topics.append((tid, title))
            if len(result.topics) < 100 or len(all_topics) >= limit:
                break
            last = result.topics[-1]
            offset_topic = last.id
            offset_id = getattr(last, "top_message", 0) or 0
            offset_date = getattr(last, "date", None)

        # No re-sort — keep Telegram's own order (pinned-first, then
        # last-activity), which is exactly what the group's topic list
        # shows at the top right now.
        return all_topics[:limit]
    finally:
        await client.disconnect()


async def extract_polls_by_topic(client, entity, channel, topic_id: int, progress_cb=None, checkpoint_cb=None) -> list:
    """
    একটা connected+entity-resolved client দিয়ে, Telegram-এর নিজের server-side
    reply_to=topic_id filter ব্যবহার করে সরাসরি ওই topic-এর message গুলো
    scan করে quiz polls বের করে। Batch of 15 kore guaranteed vote+confirm
    kore process kore — kono poll bot na-check kore skip hoy na.
    checkpoint_cb(polls_so_far, is_final) — boro topic e crash hole ওই
    porjonto CSV পাঠানোর জন্য।
    """
    polls = PollList()
    manual_review_ids = []
    checked = 0

    quiz_messages = []
    seen_ids = set()  # duplicate-guard, jodi resume-e overlap hoy
    collect_attempts = 0
    resume_from_msg_id = None  # None mane shuru theke, offset_id set korle oi id-er por theke
    while True:
        try:
            iter_kwargs = {"reply_to": topic_id, "reverse": True}
            if resume_from_msg_id is not None:
                iter_kwargs["min_id"] = resume_from_msg_id
            async for message in client.iter_messages(entity, **iter_kwargs):
                checked += 1
                resume_from_msg_id = message.id
                if message.id in seen_ids:
                    continue
                seen_ids.add(message.id)
                if not message.poll or not getattr(message.poll.poll, "quiz", False):
                    if progress_cb:
                        await progress_cb(checked, len(polls))
                    continue
                quiz_messages.append(message)
            break
        except FloodWaitError as fw:
            logger.warning(f"[poll_extract] topic-scan FloodWait {fw.seconds}s — wait kore resume korbo")
            _rate_limiter.register_flood_wait(fw.seconds)
            await asyncio.sleep(fw.seconds + 1)
        except Exception as e:
            collect_attempts += 1
            logger.warning(f"[poll_extract] topic-scan error (attempt {collect_attempts}): {type(e).__name__}: {e} — resume korbo")
            await asyncio.sleep(2.0 * collect_attempts)
            if collect_attempts >= 10:
                raise Exception(f"Topic message scan {collect_attempts} bar fail holo: {e}")

    BATCH_SIZE = 15
    MEMORY_LIMIT_MB = 10000  # 16GB RAM er beshirbhag, ei level cross korle checkpoint CSV pathabe (safety net)
    checkpoint_sent_for_level = 0

    try:
        for batch_start in range(0, len(quiz_messages), BATCH_SIZE):
            batch = quiz_messages[batch_start:batch_start + BATCH_SIZE]
            for message in batch:
                entry, resolved = await _process_single_poll(client, channel, message)
                polls.append(entry)
                if not resolved:
                    manual_review_ids.append(message.id)

                if progress_cb:
                    await progress_cb(checked, len(polls))
                await asyncio.sleep(_rate_limiter.get_delay())

                if checkpoint_cb:
                    mem_mb = _get_process_memory_mb()
                    if mem_mb and mem_mb >= MEMORY_LIMIT_MB * (checkpoint_sent_for_level + 1):
                        checkpoint_sent_for_level += 1
                        logger.warning(f"[poll_extract] memory {mem_mb:.0f}MB cross korlo — checkpoint CSV pathano hocche ({len(polls)} polls)")
                        try:
                            await checkpoint_cb(list(polls), False)
                        except Exception as cb_err:
                            logger.warning(f"[poll_extract] checkpoint_cb error: {cb_err}")

            if batch_start + BATCH_SIZE < len(quiz_messages):
                await asyncio.sleep(max(2.0, _rate_limiter.get_delay() * 3))
    except Exception as e:
        logger.error(f"[poll_extract] topic batch loop crashed mid-way at {len(polls)} polls: {type(e).__name__}: {e}")
        if checkpoint_cb and polls:
            try:
                await checkpoint_cb(list(polls), True)
            except Exception:
                pass
        e.partial_polls = list(polls)
        raise

    polls.skipped_ids = manual_review_ids
    return polls




async def get_topic_msg_range(channel, topic_id: int):
    """
    একটা topic এর first ও last message id বের করে (poll scan range এর জন্য)।
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
    await client.connect()
    try:
        try:
            entity = await client.get_entity(channel)
        except (ValueError, TypeError):
            await client.get_dialogs(limit=200)
            entity = await client.get_entity(channel)

        first_id = None
        last_id = None
        async for message in client.iter_messages(entity, reply_to=topic_id, reverse=True):
            if first_id is None:
                first_id = message.id
            last_id = message.id
        if first_id is None or first_id > topic_id:
            first_id = topic_id
        return first_id, last_id
    finally:
        await client.disconnect()


def build_topic_link(channel, topic_id: int) -> str:
    if isinstance(channel, str):
        return f"https://t.me/{channel}/{topic_id}"
    # channel may be a Telethon entity object (Chat/Channel) — extract
    # its numeric id, not str(obj) which dumps the full repr.
    ch_id = getattr(channel, "id", channel)
    ch_str = str(ch_id).replace("-100", "")
    return f"https://t.me/c/{ch_str}/{topic_id}"


async def resolve_group_ref(group_ref: str):
    """
    Group link কে Telethon channel entity তে resolve করে। সাপোর্ট করে:
    - t.me/username (public)
    - t.me/c/123456 (private, numeric id)
    - t.me/+HASH বা t.me/joinchat/HASH (private invite link — bot/session
      account কে আগে থেকে ওই গ্রুপে join থাকতে হবে; শুধু invite hash দিয়ে
      না-জয়েন-করা গ্রুপ resolve করা যায় না)
    Returns entity (channel/chat object) বা None।
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.functions.messages import CheckChatInviteRequest

    group_ref = group_ref.strip()

    # Private invite link: t.me/+HASH or t.me/joinchat/HASH
    m_invite = re.search(r"t\.me/(?:\+|joinchat/)([A-Za-z0-9_-]+)", group_ref)
    if m_invite:
        invite_hash = m_invite.group(1)
        client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
        await client.connect()
        try:
            result = await client(CheckChatInviteRequest(invite_hash))
            # ChatInviteAlready (already a member) has .chat; ChatInvite
            # (not yet a member) has no direct entity — session account
            # must already be in the group for /ok to scan its history.
            chat = getattr(result, "chat", None)
            if chat is not None:
                return chat
            return None
        except Exception as e:
            logger.error(f"[resolve_group_ref] invite check failed: {e}")
            return None
        finally:
            await client.disconnect()

    # Public username or private numeric id
    m = re.search(r"(?:t\.me/c/|t\.me/)([A-Za-z0-9_]+|\d+)", group_ref)
    if not m:
        return None
    raw = m.group(1)
    return int(f"-100{raw}") if raw.isdigit() else raw


# ── /ok <range> command (per-topic CSV → DM) ─────────────────
async def handle_ok_topic_range(msg: dict, group_ref: str, start_n: int, end_n: int):
    """
    /ok 1-5
    <group link>

    Group এর উপর থেকে নিচে first N topics নিয়ে প্রতিটার সব quiz poll
    আলাদা CSV বানিয়ে bot এর DM (owner) এ পাঠায়, caption এ topic
    name + link সহ।
    """
    from core import send_msg, edit_msg, send_document, OWNER_ID

    chat_id = msg["chat"]["id"]

    if not SESSION_STR:
        await send_msg(chat_id, "❌ SESSION_STRING set নেই। HF Space secrets এ add করো।")
        return

    channel = await resolve_group_ref(group_ref)
    if channel is None:
        await send_msg(chat_id,
            "❌ Group link resolve করা যায়নি।\n\n"
            "📌 Private invite link (t.me/+...) হলে session account টা "
            "আগে থেকেই ওই গ্রুপে join করা থাকতে হবে — না থাকলে join "
            "করিয়ে আবার try করো।")
        return

    status = await send_msg(chat_id, "⏳ Topics list করছি (group এর)...")
    status_id = status.get("result", {}).get("message_id")

    all_topics = None
    for attempt in range(3):
        try:
            all_topics = await get_forum_topics_ordered(channel)
            break
        except Exception as e:
            logger.error(f"[ok-range] topic list attempt {attempt+1} error: {e}")
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
    if not all_topics:
        await send_msg(chat_id, "❌ Topics list করা যায়নি (৩ বার চেষ্টার পরেও)।")
        return

    selected = all_topics[start_n - 1:end_n]
    if not selected:
        await send_msg(chat_id, f"❌ {start_n}-{end_n} range এ কোনো topic নাই (মোট {len(all_topics)} টা topic আছে)।")
        return

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    shared_client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
    await shared_client.connect()
    try:
        try:
            shared_entity = await shared_client.get_entity(channel)
        except (ValueError, TypeError):
            await shared_client.get_dialogs(limit=200)
            shared_entity = await shared_client.get_entity(channel)
    except Exception as e:
        await send_msg(chat_id, f"❌ Channel entity resolve করা যায়নি: {e}")
        await shared_client.disconnect()
        return

    try:
        for idx, (topic_id, topic_title) in enumerate(selected, start=start_n):
            if status_id:
                await edit_msg(chat_id, status_id, f"⏳ Topic {idx}/{end_n}: {topic_title} — scan করছি...")

            _last_edit_ts = [0.0]

            async def _progress(checked, found, _idx=idx, _title=topic_title, _ts=_last_edit_ts):
                now = time.time()
                if now - _ts[0] < 1.1:
                    return
                _ts[0] = now
                if status_id:
                    await edit_msg(chat_id, status_id,
                        f"⏳ Topic {_idx}/{end_n}: {_title}\n"
                        f"📨 চেক: {checked} messages\n"
                        f"📋 Poll পেয়েছি: {found}")

            polls = PollList()
            polls.skipped_ids = []
            last_partial = None
            extraction_succeeded = False
            for attempt in range(5):
                try:
                    polls = await extract_polls_by_topic(shared_client, shared_entity, channel, topic_id, progress_cb=_progress)
                    extraction_succeeded = True
                    break
                except Exception as e:
                    partial = getattr(e, "partial_polls", None)
                    if partial:
                        last_partial = partial  # last attempt-er partial-i rakhbo, prottekbar accumulate na kore (duplicate avoid)
                    logger.error(f"[ok-range] topic {topic_id} extract attempt {attempt+1} error: {e}")
                    if attempt < 4:
                        await send_msg(chat_id, f"⚠️ Topic '{topic_title}' এ সমস্যা হয়েছিল, auto-retry করছি ({attempt+2}/5)...")
                        await asyncio.sleep(3 * (attempt + 1))
                    else:
                        # 5 bar-i fail — last partial-take best-effort hishebe use koro
                        if last_partial:
                            polls = PollList()
                            polls.extend(last_partial)
                            polls.skipped_ids = []
                            extraction_succeeded = True  # partial data-o legitimate result, "no polls" na "error" hishebe treat na kore
            if not extraction_succeeded:
                await send_msg(chat_id, f"⚠️ Topic '{topic_title}' scan এ error (৫ বার চেষ্টার পরেও fail)। পরের topic এ যাচ্ছি।")
                continue

            if not polls:
                await send_msg(chat_id, f"😕 Topic '{topic_title}' এ কোনো quiz poll পাওয়া যায়নি।")
                continue

            csv_bytes = build_csv(polls)
            safe_title = re.sub(r"[^A-Za-z0-9\-]+", "_", topic_title.encode("ascii", "ignore").decode("ascii")) or "topic"
            safe_title = safe_title[:50].strip("_") or "topic"
            filename = f"{safe_title}_{topic_id}.csv"
            topic_link = build_topic_link(channel, topic_id)

            caption = (
                f"📌 <b>{topic_title}</b>\n"
                f"🔗 {topic_link}\n"
                f"📋 প্রশ্ন: {len(polls)}"
            )
            caption += _skipped_note(polls)

            sent = False
            for attempt in range(3):
                try:
                    doc_result = await send_document(OWNER_ID, csv_bytes, filename, caption=caption, mime_type="text/csv")
                    if doc_result and doc_result.get("ok"):
                        sent = True
                        break
                    logger.warning(f"[ok-range] send_document non-ok (attempt {attempt+1}) for topic {topic_id}: {doc_result}")
                except Exception as e:
                    logger.error(f"[ok-range] DM send attempt {attempt+1} error for topic {topic_id}: {e}")
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
            if not sent:
                await send_msg(chat_id, f"⚠️ Topic '{topic_title}' এর CSV DM এ পাঠাতে ব্যর্থ (৩ বার চেষ্টার পরেও)।")
    finally:
        await shared_client.disconnect()

    if status_id:
        await edit_msg(chat_id, status_id, f"✅ সম্পন্ন! {len(selected)} টা topic প্রসেস হয়েছে।")


# ── /ok <single topic link> mode (one topic → CSV → DM) ──────
async def handle_ok_single_topic(msg: dict, topic_link: str):
    """
    /ok
    https://t.me/c/123456/45   (or public: https://t.me/mychan/45)

    লিংকে থাকা topic_id ধরে নিয়ে সেই টপিকের সব quiz poll বের করে CSV
    বানিয়ে bot এর DM (owner) এ পাঠায়, caption এ topic name + link সহ —
    ঠিক /ok N-M মোডে প্রতিটা topic এর জন্য যা হয় তারই single-topic version।
    """
    from core import send_msg, edit_msg, send_document, OWNER_ID
    import time as _overall_time
    overall_start = _overall_time.monotonic()

    chat_id = msg["chat"]["id"]

    if not SESSION_STR:
        await send_msg(chat_id, "❌ SESSION_STRING set নেই। HF Space secrets এ add করো।")
        return

    ch, num, topic_from_parse = parse_tg_link(topic_link)
    if not ch or not num:
        await send_msg(chat_id, "❌ Link parse হয়নি। সঠিক topic link দাও (যেমন t.me/c/123/45)।")
        return

    # A bare "t.me/group/45" or "t.me/c/123/45" link — the trailing number IS
    # the topic id itself (topic root message), not topic_id+msg_id together.
    topic_id = topic_from_parse if topic_from_parse else num

    status = await send_msg(chat_id, f"⏳ Topic {topic_id} scan করছি...")
    status_id = status.get("result", {}).get("message_id")

    _last_edit_ts = [0.0]

    async def _progress(checked, found):
        now = time.time()
        if now - _last_edit_ts[0] < 1.1:
            return
        _last_edit_ts[0] = now
        if status_id:
            await edit_msg(chat_id, status_id,
                f"⏳ Topic {topic_id}\n"
                f"📨 চেক: {checked} messages (topic-only)\n"
                f"📋 Poll পেয়েছি: {found}")

    async def _checkpoint(polls_so_far, is_final):
        if not polls_so_far:
            return
        try:
            interim_csv = build_csv(polls_so_far)
            label = "🛑 বিপদ! এই পর্যন্ত যা পেয়েছি:" if is_final else "💾 Checkpoint:"
            await send_document(
                chat_id, interim_csv, f"CHECKPOINT_topic{topic_id}_upto{len(polls_so_far)}.csv",
                caption=f"{label}\n📋 Poll: <b>{len(polls_so_far)}</b>\nTopic: {topic_id}",
                mime_type="text/csv"
            )
        except Exception as e:
            logger.error(f"[ok-single] checkpoint send failed: {e}")

    # extract_polls_by_topic Telegram-er nijer server-side reply_to filter
    # use kore — pura channel-er range scan kore na, shudhu ei topic-er
    # message-i direct fetch kore. Onek beshi efficient boro channel-e।
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    tclient = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
    await tclient.connect()
    try:
        try:
            entity = await tclient.get_entity(ch)
        except (ValueError, TypeError):
            await tclient.get_dialogs(limit=200)
            entity = await tclient.get_entity(ch)

        last_partial = None
        extraction_succeeded = False
        polls = PollList()
        for attempt in range(5):
            try:
                polls = await extract_polls_by_topic(tclient, entity, ch, topic_id, progress_cb=_progress, checkpoint_cb=_checkpoint)
                extraction_succeeded = True
                break
            except Exception as e:
                partial = getattr(e, "partial_polls", None)
                if partial:
                    last_partial = partial
                logger.error(f"[ok-single] topic {topic_id} extract attempt {attempt+1} error: {e}")
                if attempt < 4:
                    await send_msg(chat_id, f"⚠️ সমস্যা হয়েছিল, auto-retry করছি ({attempt+2}/5)...")
                    await asyncio.sleep(3 * (attempt + 1))
                else:
                    if last_partial:
                        polls = PollList()
                        polls.extend(last_partial)
                        polls.skipped_ids = []
                        extraction_succeeded = True
    finally:
        await tclient.disconnect()

    if not extraction_succeeded:
        await send_msg(chat_id, f"❌ Topic {topic_id} scan এ error (৫ বার চেষ্টার পরেও fail)।")
        return

    if not polls:
        await send_msg(chat_id, f"😕 এই topic এ কোনো quiz poll পাওয়া যায়নি।")
        return

    # Try to get the actual topic title via forum topics list (falls back to
    # a generic label if lookup fails — extraction itself still succeeds).
    topic_title = f"Topic {topic_id}"
    try:
        all_topics = await get_forum_topics_ordered(ch)
        for tid, title in all_topics:
            if tid == topic_id:
                topic_title = title
                break
    except Exception as e:
        logger.warning(f"[ok-single] topic title lookup failed: {e}")

    csv_bytes = build_csv(polls)
    safe_title = re.sub(r"[^A-Za-z0-9\-]+", "_", topic_title.encode("ascii", "ignore").decode("ascii")) or "topic"
    safe_title = safe_title[:50].strip("_") or "topic"
    filename = f"{safe_title}_{topic_id}.csv"
    built_link = build_topic_link(ch, topic_id)

    total_elapsed = _overall_time.monotonic() - overall_start
    time_display = _format_elapsed(total_elapsed)

    caption = (
        f"📌 <b>{topic_title}</b>\n"
        f"🔗 {built_link}\n"
        f"📋 প্রশ্ন: {len(polls)}\n"
        f"⏱️ মোট সময়: {time_display}"
    )
    caption += _skipped_note(polls)

    if status_id:
        await edit_msg(chat_id, status_id, f"⏳ CSV পাঠাচ্ছি ({len(polls)}টি প্রশ্ন)...")

    sent = False
    for attempt in range(3):
        try:
            doc_result = await send_document(OWNER_ID, csv_bytes, filename, caption=caption, mime_type="text/csv")
            if doc_result and doc_result.get("ok"):
                sent = True
                break
            logger.warning(f"[ok-single] send_document non-ok (attempt {attempt+1}): {doc_result}")
        except Exception as e:
            logger.error(f"[ok-single] DM send attempt {attempt+1} error: {e}")
        if attempt < 2:
            await asyncio.sleep(2 * (attempt + 1))
    if not sent:
        await send_msg(chat_id, "⚠️ CSV DM এ পাঠাতে ব্যর্থ (৩ বার চেষ্টার পরেও)।")
        return

    if status_id:
        await edit_msg(chat_id, status_id, f"✅ সম্পন্ন! {len(polls)}টি প্রশ্ন DM এ পাঠানো হয়েছে।")


# ── pinned-message helpers (topic root + summary already-done check) ──
async def _get_topic_pinned_texts(client, entity, topic_id: int) -> list:
    """Ei topic-e ekhon jotogulo pinned message ache, tader (msg_id, text) list dey.
    top_msg_id diye search scope kora hoy, tai pura group na, ei topic-i check hoy."""
    from telethon.tl.functions.messages import SearchRequest
    from telethon.tl.types import InputMessagesFilterPinned
    try:
        res = await client(SearchRequest(
            peer=entity, q="", filter=InputMessagesFilterPinned(),
            min_date=None, max_date=None, offset_id=0, add_offset=0,
            limit=100, max_id=0, min_id=0, hash=0, top_msg_id=topic_id,
        ))
        out = []
        for m in getattr(res, "messages", []):
            out.append((getattr(m, "id", None), getattr(m, "message", "") or ""))
        return out
    except Exception as e:
        logger.warning(f"[ok-all] pinned-check failed for topic {topic_id}: {e}")
        return []


_SUMMARY_MARK = "🌟মোট প্রশ্ন"  # build_ok_summary always starts with this — pinned summary detect korar jonno


def _bot_chat_id(channel):
    """resolve_group_ref theke asha channel Telethon Channel/Chat object
    hote pare (invite-link case), ba str/int (username/numeric id) hote
    pare. tg_post (Bot API)-e always plain str/int chat_id lagbe — object
    dile JSON serialize e crash kore, tai eikhane normalize kora hoy."""
    if isinstance(channel, (str, int)):
        return channel
    ch_id = getattr(channel, "id", None)
    if ch_id is not None:
        return int(f"-100{ch_id}")
    return channel


async def handle_ok_all_topics(msg: dict, group_ref: str):
    """
    /ok
    <group link>   (no range, no topic — just the group, bot admin ache emon)

    Group-er প্রতিটা topic-e ঢুকে:
      1. Topic-er first post pin kore (age theke pinned thakle skip)
      2. Batch-scan diye summary post banay, group-e post kore pin kore
         (age theke summary pin kora thakle notun kore banay na)
    DM-e (owner) shudhu progress update jay: kon topic cholche, kotogulo
    shesh, koto % holo. Shob topic shesh hole ekta master summary DM-e
    jay — protyek topic-er naam + tar summary post-er (batch/part) link.
    """
    from core import send_msg, edit_msg, OWNER_ID, tg_post

    chat_id = msg["chat"]["id"]

    if not SESSION_STR:
        await send_msg(chat_id, "❌ SESSION_STRING set নেই। HF Space secrets এ add করো।")
        return

    channel = await resolve_group_ref(group_ref)
    if channel is None:
        await send_msg(chat_id,
            "❌ Group link resolve করা যায়নি।\n\n"
            "📌 Private invite link (t.me/+...) হলে session account টা "
            "আগে থেকেই ওই গ্রুপে join করা থাকতে হবে।")
        return

    status = await send_msg(chat_id, "⏳ সব topics list করছি...")
    status_id = status.get("result", {}).get("message_id")

    all_topics = None
    for attempt in range(3):
        try:
            all_topics = await get_forum_topics_ordered(channel)
            break
        except Exception as e:
            logger.error(f"[ok-all] topic list attempt {attempt+1} error: {e}")
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
    if not all_topics:
        await send_msg(chat_id, "❌ Topics list করা যায়নি (৩ বার চেষ্টার পরেও)।")
        return

    total_topics = len(all_topics)
    done_count = 0
    bot_chat = _bot_chat_id(channel)
    # final master-DM er jonno: [(topic_title, topic_link, [batch_link, ...]), ...]
    results = []

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    shared_client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
    await shared_client.connect()
    try:
        try:
            shared_entity = await shared_client.get_entity(channel)
        except (ValueError, TypeError):
            await shared_client.get_dialogs(limit=200)
            shared_entity = await shared_client.get_entity(channel)
    except Exception as e:
        await send_msg(chat_id, f"❌ Channel entity resolve করা যায়নি: {e}")
        await shared_client.disconnect()
        return

    try:
        for idx, (topic_id, topic_title) in enumerate(all_topics, start=1):
            pct = int((idx - 1) / total_topics * 100)
            await send_msg(chat_id, f"⏳ ({pct}%) Topic {idx}/{total_topics}: {topic_title} — কাজ শুরু...")

            topic_link = build_topic_link(channel, topic_id)

            # ── ইতিমধ্যে কী কী pinned আছে চেক করো (duplicate pin/summary এড়াতে) ──
            pinned = await _get_topic_pinned_texts(shared_client, shared_entity, topic_id)
            root_already_pinned = any(pid == topic_id for pid, _ in pinned)
            existing_summary_id = next((pid for pid, txt in pinned if txt.startswith(_SUMMARY_MARK)), None)

            # ── ১. প্রথম post pin (আগে না থাকলে) ──
            # topic_id nijei "topic created" service message — Bot API
            # service message pin korte dey na, tai actual first REAL
            # message ber kore ta pin korbo (get_topic_msg_range diye)।
            first_real_id, _last_id_unused = await get_topic_msg_range(channel, topic_id)
            root_target_id = first_real_id if first_real_id and first_real_id != topic_id else None
            root_already_pinned = root_already_pinned or (
                root_target_id is not None and any(pid == root_target_id for pid, _ in pinned)
            )
            if not root_already_pinned and root_target_id:
                r = await tg_post("pinChatMessage", {
                    "chat_id": bot_chat, "message_id": root_target_id, "disable_notification": True
                })
                if not r or not r.get("ok"):
                    logger.warning(f"[ok-all] first-post pin failed topic {topic_id}: {(r or {}).get('description')}")

            # ── ২. Summary post (batch-scan) — আগে থেকে pinned না থাকলে বানাও ──
            batch_links = []
            if existing_summary_id:
                # age theke summary ache — regenerate na kore purono link use korbo
                batch_links = ["(আগে থেকেই pin করা আছে)"]
            else:
                first_id, last_id = first_real_id, _last_id_unused
                if first_id and last_id:
                    try:
                        batches = await scan_poll_batches_telethon(channel, first_id, last_id, topic_id=topic_id)
                    except Exception as e:
                        logger.error(f"[ok-all] batch scan failed topic {topic_id}: {e}")
                        batches = []
                    if batches:
                        total_polls = sum(c for _, c in batches)
                        batches_with_links = [
                            (i + 1, build_batch_link(channel, first_bid, topic_id), count)
                            for i, (first_bid, count) in enumerate(batches)
                        ]
                        batch_links = [ln for _, ln, _ in batches_with_links]
                        summary_text = build_ok_summary(total_polls, batches_with_links)
                        post_params = {
                            "chat_id": bot_chat, "text": summary_text, "parse_mode": "Markdown",
                            "disable_web_page_preview": True, "message_thread_id": topic_id,
                        }
                        r = await tg_post("sendMessage", post_params)
                        if r and r.get("ok"):
                            sent_msg_id = r["result"]["message_id"]
                            await tg_post("pinChatMessage", {
                                "chat_id": bot_chat, "message_id": sent_msg_id, "disable_notification": True
                            })
                        else:
                            logger.warning(f"[ok-all] summary post failed topic {topic_id}: {(r or {}).get('description')}")
                            batch_links = []

            results.append((topic_title, topic_link, batch_links))
            done_count += 1
            pct_now = int(done_count / total_topics * 100)
            await send_msg(chat_id, f"✅ ({pct_now}%) Topic '{topic_title}' শেষ। ({done_count}/{total_topics})")
    finally:
        await shared_client.disconnect()

    if status_id:
        await edit_msg(chat_id, status_id, f"✅ সম্পন্ন! মোট {total_topics} টা topic প্রসেস হয়েছে।")

    # ── Master summary DM: প্রতি topic নাম + তার summary/batch link(s) ──
    lines = [f"🌟 সব topic শেষ! ({total_topics} টা)\n"]
    for topic_title, topic_link, batch_links in results:
        lines.append(f"📌 <b>{topic_title}</b>\n🔗 {topic_link}")
        if not batch_links:
            lines.append("— (কোনো quiz poll পাওয়া যায়নি)")
        elif len(batch_links) == 1:
            lines.append(batch_links[0])
        else:
            for i, ln in enumerate(batch_links, start=1):
                lines.append(f"Part-{i:02d}: {ln}")
        lines.append("")

    final_text = "\n".join(lines)
    # Telegram single-message length limit — dorkar hole split kore পাঠাও
    CHUNK = 3500
    chunks = [final_text[i:i+CHUNK] for i in range(0, len(final_text), CHUNK)] or [final_text]
    for chunk in chunks:
        await send_msg(OWNER_ID, chunk, parse_mode="HTML")


# ── /ok handler ───────────────────────────────────────────────
async def handle_ok_command(msg: dict):
    """
    /ok
    https://t.me/c/.../101
    https://t.me/c/.../250

    Range এর মধ্যে প্রতিটা batch এর first poll link নিয়ে
    master-summary style এ একটা summary message পাঠায়।
    """
    from core import send_msg, edit_msg

    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()

    body = re.sub(r"^/ok\s*", "", text, flags=re.IGNORECASE).strip()
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    links = [l for l in lines if "t.me/" in l]

    if len(links) < 2:
        await send_msg(chat_id,
            "❌ দুটো link দাও!\n\n"
            "📌 Format:\n"
            "<code>/ok\n"
            "https://t.me/c/.../101\n"
            "https://t.me/c/.../250</code>",
            parse_mode="HTML"
        )
        return

    ch1, start_id, topic1 = parse_tg_link(links[0])
    ch2, end_id, topic2 = parse_tg_link(links[1])

    if not ch1 or not start_id or not end_id:
        await send_msg(chat_id, "❌ Link parse হয়নি। সঠিক Telegram link দাও।")
        return
    if ch1 != ch2:
        await send_msg(chat_id, "❌ দুটো link একই channel/group এর হতে হবে!")
        return

    topic_id = topic1 or topic2
    if start_id > end_id:
        start_id, end_id = end_id, start_id

    total = end_id - start_id + 1
    if total > 3000:
        logger.info(f"[poll_extract] Large range requested: {total} messages ({start_id}-{end_id}) — proceeding without cap")
    if not SESSION_STR:
        await send_msg(chat_id, "❌ SESSION_STRING set নেই। HF Space secrets এ add করো।")
        return

    r = await send_msg(chat_id, f"⏳ Scan করছি: {start_id} → {end_id} ({total} messages)...")
    status_id = r.get("result", {}).get("message_id")

    async def progress(checked, found, elapsed=None):
        if status_id:
            await edit_msg(chat_id, status_id, f"⏳ চেক: {checked}/{total} — Batch পেয়েছি: {found}")

    try:
        batches = await scan_poll_batches_telethon(ch1, start_id, end_id, progress_cb=progress, topic_id=topic_id)
    except Exception as e:
        logger.error(f"[ok] Telethon error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")
        return

    if not batches:
        await send_msg(chat_id, f"😕 এই range এ কোনো quiz poll batch পাওয়া যায়নি।\n({total} messages চেক হয়েছে)")
        return

    total_polls = sum(c for _, c in batches)
    batches_with_links = [
        (i + 1, build_batch_link(ch1, first_id, topic_id), count)
        for i, (first_id, count) in enumerate(batches)
    ]

    summary = build_ok_summary(total_polls, batches_with_links)

    if status_id:
        await edit_msg(chat_id, status_id, "✅ সম্পন্ন!")

    # Post the summary into the source channel/topic itself (not just the
    # invoking chat), then try to pin it — pin silently no-ops (with an
    # owner notification) if the bot lacks admin/pin rights there.
    from core import tg_post
    from app import try_pin_message
    post_params = {"chat_id": ch1, "text": summary, "parse_mode": "Markdown",
                   "disable_web_page_preview": True}
    if topic_id:
        post_params["message_thread_id"] = topic_id
    r = await tg_post("sendMessage", post_params)
    if r and r.get("ok"):
        sent_msg_id = r["result"]["message_id"]
        await try_pin_message(ch1, sent_msg_id)
        await send_msg(chat_id, f"✅ Summary পাঠানো হয়েছে ও pin করা হয়েছে (admin access থাকলে)।\n\n{summary}", parse_mode="Markdown")
    else:
        err = (r or {}).get("description", "unknown error")
        await send_msg(chat_id, f"⚠️ Channel এ summary পাঠাতে ব্যর্থ: {err}\n\n{summary}", parse_mode="Markdown")

