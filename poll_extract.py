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
from io import StringIO

logger = logging.getLogger("atlas.poll_extract")

API_ID       = int(os.environ.get("API_ID", "33312774"))
API_HASH     = os.environ.get("API_HASH", "883db3366f8759d1d14c861c0d628232")
SESSION_STR  = os.environ.get("SESSION_STRING", "")


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
async def extract_polls_telethon(channel, start_id: int, end_id: int, progress_cb=None, topic_id=None) -> list:
    """
    Telethon দিয়ে channel থেকে start_id→end_id range এর
    সব quiz poll extract করে list of dict return করে।
    GetPollResultsRequest ব্যবহার করে vote ছাড়াই correct answer পায়।
    progress_cb(checked, found) — optional callback every 100 msgs
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl import functions

    polls = []
    client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
    await client.connect()

    try:
        checked = 0
        async for message in client.iter_messages(
            channel,
            min_id=start_id - 1,
            max_id=end_id + 1,
            limit=end_id - start_id + 1,
            reverse=True,
        ):
            checked += 1

            # Topic filter — group topic এর message হলে reply_to check করো
            if topic_id and message.reply_to:
                msg_topic = getattr(message.reply_to, "reply_to_top_id", None) or getattr(message.reply_to, "reply_to_msg_id", None)
                if msg_topic != topic_id:
                    continue
            elif topic_id and not message.reply_to:
                continue

            if not message.poll:
                if progress_cb and checked % 100 == 0:
                    await progress_cb(checked, len(polls))
                continue

            p = message.poll.poll

            # Quiz poll only (non-quiz poll এ correct answer নেই)
            if not getattr(p, "quiz", False):
                if progress_cb and checked % 100 == 0:
                    await progress_cb(checked, len(polls))
                continue

            # Question text
            q_text = p.question.text if hasattr(p.question, "text") else str(p.question)

            # [SABAS] বা যেকোনো case → [ATLAS] replace
            q_text = re.sub(r'\[sabas\]', '[ATLAS]', q_text, flags=re.IGNORECASE)
            q_text = re.sub(r'\bsabas\b', 'ATLAS', q_text, flags=re.IGNORECASE)

            # Options
            options = []
            for ans in p.answers:
                opt = ans.text.text if hasattr(ans.text, "text") else str(ans.text)
                options.append(opt)

            # ── Correct answer ──
            correct_idx = 0
            explanation = ""
            try:
                results = message.poll.results

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

                correct_idx, explanation, found = _parse_results(results)

                if not found:
                    # Vote দাও → message refetch করো
                    try:
                        await client(functions.messages.SendVoteRequest(
                            peer=channel,
                            msg_id=message.id,
                            options=[p.answers[0].option]
                        ))
                        await asyncio.sleep(0.4)
                    except Exception:
                        pass  # Already voted — ok

                    # Refetch message — এখন correct flag থাকবে
                    fetched = await client.get_messages(channel, ids=message.id)
                    if fetched and fetched.poll:
                        correct_idx, explanation, _ = _parse_results(fetched.poll.results)

            except Exception as e:
                logger.warning(f"[poll_extract] msg {message.id}: {type(e).__name__}: {e}")

            polls.append({
                "question":    q_text,
                "options":     options,
                "correct_idx": correct_idx,       # 0-based
                "answer":      correct_idx + 1,   # 1-based for CSV
                "explanation": explanation,
            })

            if progress_cb and checked % 100 == 0:
                await progress_cb(checked, len(polls))

            # Rate limit এড়াতে ছোট delay
            await asyncio.sleep(0.05)

    finally:
        await client.disconnect()

    return polls


# ── CSV builder ──────────────────────────────────────────────
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

    # Secondary Supabase (backup of backup)
    try:
        SB2_URL = "https://xnkuuzstschdovcyomfk.supabase.co"
        SB2_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inhua3V1enN0c2NoZG92Y3lvbWZrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI3NTI3NzUsImV4cCI6MjA5ODMyODc3NX0.rD6p4U1fdqnM2M6t7wA3qsMY1p3KEFD2S1WzSIZehW4"
        headers2 = {
            "apikey": SB2_KEY,
            "Authorization": f"Bearer {SB2_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{SB2_URL}/rest/v1/quiz_backups", headers=headers2, json=payload)
        logger.info(f"[poll_extract] Supabase Secondary backup ok: {quiz_id}")
    except Exception as e:
        logger.warning(f"[poll_extract] Supabase Secondary backup failed: {e}")

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
    if total > 1000:
        await send_msg(chat_id, f"❌ Range বড় ({total})। সর্বোচ্চ ১০০০ রাখো।")
        return

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

    QUIZ_WORKER_URL = "https://atlasquizbotpro.hamza818483.workers.dev"
    GH_PAGES_URL    = "https://hamza818483-dotcom.github.io/QuizBot/quiz.html"
    HF_SPACE_URL    = "https://quizbot-s482.onrender.com"  # v4.2: HF permanently banned, Render primary

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
