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
class PollList(list):
    """Plain list e attribute set kora jay na (AttributeError) — tai
    ei subclass use kore, jate polls.skipped_ids reliably kaj kore."""
    skipped_ids: list = []


async def extract_polls_telethon(channel, start_id: int, end_id: int, progress_cb=None, topic_id=None) -> list:
    """
    Telethon দিয়ে channel থেকে start_id→end_id range এর
    সব quiz poll extract করে list of dict return করে।
    Batch of 15 poll ekbare guaranteed vote+confirm kore process kore —
    kono poll silently skip hoy na, na parle "MANUALLY VERIFY" mark kore rakhe.
    progress_cb(checked, found) — optional callback every 100 msgs
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

        # ── Step 2: batch of 15 kore guaranteed vote+confirm process ──
        BATCH_SIZE = 15
        for batch_start in range(0, len(quiz_messages), BATCH_SIZE):
            batch = quiz_messages[batch_start:batch_start + BATCH_SIZE]
            for message in batch:
                entry, resolved = await _process_single_poll(client, channel, message)
                if resolved:
                    polls.append(entry)
                else:
                    manual_review_ids.append(message.id)
                    polls.append(entry)  # rakhbo, kintu MANUALLY VERIFY mark shoho

                if progress_cb:
                    await progress_cb(checked, len(polls))
                await asyncio.sleep(0.15)

            # Batch er por ektu beshi break — rate-limit/timing safety
            if batch_start + BATCH_SIZE < len(quiz_messages):
                await asyncio.sleep(1.2)

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
        try:
            vote_res = await client(functions.messages.SendVoteRequest(
                peer=channel,
                msg_id=message.id,
                options=[p.answers[0].option]
            ))
            vote_poll_results = _extract_poll_results_from_updates(vote_res)
            if vote_poll_results:
                correct_idx, explanation, found = _parse_results(vote_poll_results)
        except Exception:
            pass  # Already voted — ok, fallback to refetch below

        if found:
            break

        # Strategy 2: message refetch kore poll.results check
        wait = min(1.0 + attempt * 0.5, max_wait)
        await asyncio.sleep(wait)
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
    async def progress(checked, found):
        if status_id:
            await edit_msg(chat_id, status_id,
                f"⏳ চেক: {checked}/{total} — Poll পেয়েছি: {found}",
                parse_mode="HTML"
            )

    # Extract
    try:
        polls = await extract_polls_telethon(ch1, start_id, end_id, progress_cb=progress, topic_id=topic_id)
    except Exception as e:
        logger.error(f"[poll_extract] Telethon error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")
        return

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

    caption = (
        f"✅ <b>Poll Extract সম্পন্ন!</b>\n"
        f"📌 Range: {start_id} → {end_id}\n"
        f"📋 Poll পেয়েছি: <b>{len(polls)}</b>\n\n"
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


async def extract_polls_by_topic(client, entity, channel, topic_id: int, progress_cb=None) -> list:
    """
    একটা connected+entity-resolved client দিয়ে, Telegram-এর নিজের server-side
    reply_to=topic_id filter ব্যবহার করে সরাসরি ওই topic-এর message গুলো
    scan করে quiz polls বের করে। Batch of 15 kore guaranteed vote+confirm
    kore process kore — kono poll bot na-check kore skip hoy na.
    """
    polls = PollList()
    manual_review_ids = []
    checked = 0

    quiz_messages = []
    async for message in client.iter_messages(entity, reply_to=topic_id, reverse=True):
        checked += 1
        if not message.poll or not getattr(message.poll.poll, "quiz", False):
            if progress_cb:
                await progress_cb(checked, len(polls))
            continue
        quiz_messages.append(message)

    BATCH_SIZE = 15
    for batch_start in range(0, len(quiz_messages), BATCH_SIZE):
        batch = quiz_messages[batch_start:batch_start + BATCH_SIZE]
        for message in batch:
            entry, resolved = await _process_single_poll(client, channel, message)
            polls.append(entry)
            if not resolved:
                manual_review_ids.append(message.id)

            if progress_cb:
                await progress_cb(checked, len(polls))
            await asyncio.sleep(0.15)

        if batch_start + BATCH_SIZE < len(quiz_messages):
            await asyncio.sleep(1.2)

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

            polls = None
            for attempt in range(3):
                try:
                    polls = await extract_polls_by_topic(shared_client, shared_entity, channel, topic_id, progress_cb=_progress)
                    break
                except Exception as e:
                    logger.error(f"[ok-range] topic {topic_id} extract attempt {attempt+1} error: {e}")
                    if attempt < 2:
                        await asyncio.sleep(2 * (attempt + 1))
            if polls is None:
                await send_msg(chat_id, f"⚠️ Topic '{topic_title}' scan এ error (৩ বার চেষ্টার পরেও fail)।")
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

    try:
        first_id, last_id = await get_topic_msg_range(ch, topic_id)
    except Exception as e:
        logger.error(f"[ok-single] topic {topic_id} range error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")
        return

    if not last_id:
        await send_msg(chat_id, "😕 এই topic এ কোনো message নাই।")
        return

    total_msgs = last_id - first_id + 1
    _last_edit_ts = [0.0]

    async def _progress(checked, found):
        now = time.time()
        if now - _last_edit_ts[0] < 1.1 and checked < total_msgs:
            return
        _last_edit_ts[0] = now
        if status_id:
            await edit_msg(chat_id, status_id,
                f"⏳ Topic {topic_id}\n"
                f"📨 চেক: {checked}/{total_msgs} messages\n"
                f"📋 Poll পেয়েছি: {found}")

    try:
        polls = await extract_polls_telethon(ch, first_id, last_id, progress_cb=_progress, topic_id=topic_id)
    except Exception as e:
        logger.error(f"[ok-single] topic {topic_id} extract error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")
        return

    if not polls:
        await send_msg(chat_id, f"😕 এই topic এ কোনো quiz poll পাওয়া যায়নি।\n({total_msgs} messages চেক হয়েছে)")
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

    caption = (
        f"📌 <b>{topic_title}</b>\n"
        f"🔗 {built_link}\n"
        f"📋 প্রশ্ন: {len(polls)}"
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


# ── /ok <group link only> mode (ALL topics → CSV each → DM, auto-continue) ──
async def handle_ok_all_topics(msg: dict, group_ref: str):
    """
    /ok
    <group link>   (no range, no topic — just the group)

    Group এর সব topic auto scan করে, একটা শেষ হলে CSV DM এ পাঠিয়ে
    পরের topic নিজে থেকেই ধরে নেয়। কোনো topic এ error/fail হলেও
    থামবে না — retry করে (max 3 বার, backoff সহ) তারপর skip করে
    পরেরটায় চলে যায়। শেষে সব topic প্রসেস হওয়া পর্যন্ত চলতেই থাকে।
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
    done, skipped = 0, 0

    # একটাই client সব topic এর জন্য reuse করা হচ্ছে — আগে প্রতি topic এ
    # ২টা আলাদা connect+entity-resolve হতো (range বের করতে + scan করতে),
    # এখন single connect + single pass per topic। Poll parsing logic same।
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
            if status_id:
                await edit_msg(chat_id, status_id, f"⏳ Topic {idx}/{total_topics}: {topic_title} — scan করছি...")

            _last_edit_ts = [0.0]

            async def _progress(checked, found, _idx=idx, _title=topic_title, _ts=_last_edit_ts):
                now = time.time()
                if now - _ts[0] < 1.1:
                    return
                _ts[0] = now
                if status_id:
                    await edit_msg(chat_id, status_id,
                        f"⏳ Topic {_idx}/{total_topics}: {_title}\n"
                        f"📨 চেক: {checked} messages\n"
                        f"📋 Poll পেয়েছি: {found}")

            polls = None
            for attempt in range(3):
                try:
                    polls = await extract_polls_by_topic(shared_client, shared_entity, channel, topic_id, progress_cb=_progress)
                    break
                except Exception as e:
                    logger.error(f"[ok-all] topic {topic_id} extract attempt {attempt+1} error: {e}")
                    if attempt < 2:
                        await asyncio.sleep(2 * (attempt + 1))
            if polls is None:
                skipped += 1
                await send_msg(chat_id, f"⚠️ Topic '{topic_title}' skip (scan ৩ বার fail হয়েছে)।")
                continue

            if not polls:
                done += 1
                continue  # no polls found, move on silently — don't stop

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
                    logger.warning(f"[ok-all] send_document non-ok (attempt {attempt+1}) for topic {topic_id}: {doc_result}")
                except Exception as e:
                    logger.error(f"[ok-all] DM send attempt {attempt+1} error for topic {topic_id}: {e}")
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))

            if sent:
                done += 1
            else:
                skipped += 1
                await send_msg(chat_id, f"⚠️ Topic '{topic_title}' এর CSV পাঠানো যায়নি (৩ বার চেষ্টার পরেও)।")

            # auto-continue to next topic regardless of outcome — never stop
    finally:
        await shared_client.disconnect()

    if status_id:
        await edit_msg(chat_id, status_id,
            f"✅ সম্পন্ন! মোট {total_topics} টা topic এর মধ্যে {done} টা সফল"
            f"{f', {skipped} টা skip হয়েছে' if skipped else ''}।")


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

    async def progress(checked, found):
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

