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
    Returns (channel_entity, msg_id)
    Private:  t.me/c/1234567890/55  → (int(-1001234567890), 55)
    Public:   t.me/mychannel/55     → ("mychannel", 55)
    """
    link = link.strip().rstrip("/")
    m = re.search(r"t\.me/c/(\d+)/(\d+)", link)
    if m:
        return int(f"-100{m.group(1)}"), int(m.group(2))
    m = re.search(r"t\.me/([A-Za-z0-9_]+)/(\d+)", link)
    if m:
        return m.group(1), int(m.group(2))
    return None, None


# ── Telethon extract ─────────────────────────────────────────
async def extract_polls_telethon(channel, start_id: int, end_id: int, progress_cb=None) -> list:
    """
    Telethon দিয়ে channel থেকে start_id→end_id range এর
    সব quiz poll extract করে list of dict return করে।
    progress_cb(checked, found) — optional callback every 100 msgs
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

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
            if message.poll:
                p       = message.poll.poll
                results = message.poll.results

                # Question text
                q_text = p.question.text if hasattr(p.question, "text") else str(p.question)

                # Options
                options = []
                for ans in p.answers:
                    opt = ans.text.text if hasattr(ans.text, "text") else str(ans.text)
                    options.append(opt)

                # Correct answer (0-based index)
                correct_idx = 0
                if results and results.results:
                    for i, r in enumerate(results.results):
                        if getattr(r, "correct", False):
                            correct_idx = i
                            break

                # Explanation
                explanation = ""
                if results and getattr(results, "solution", None):
                    explanation = results.solution

                polls.append({
                    "question":    q_text,
                    "options":     options,
                    "correct_idx": correct_idx,       # 0-based
                    "answer":      correct_idx + 1,   # 1-based for CSV
                    "explanation": explanation,
                })

            if progress_cb and checked % 100 == 0:
                await progress_cb(checked, len(polls))

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


# ── D1 quiz save ─────────────────────────────────────────────
async def save_quiz_to_d1(polls: list, name: str, uid: int) -> str | None:
    """
    polls list → D1 quizzes table এ save করে quiz_id return করে।
    quiz_id format: qz_XXXXXXXX (existing convention থেকে)
    Returns quiz_id বা None on failure.
    """
    from core import d1_run, d1_select
    from pdf_handler import gen_session_id

    # D1 এর existing format এ convert করো
    # _parse_csv_bytes যা return করে সেই format
    questions = []
    ans_map = {0: "A", 1: "B", 2: "C", 3: "D", 4: "E"}
    for p in polls:
        opts = p["options"]
        questions.append({
            "question":    p["question"],
            "options":     opts,
            "answer":      ans_map.get(p["correct_idx"], "A"),
            "explanation": p["explanation"],
        })

    quiz_id = "qz_" + gen_session_id()[:8]

    try:
        await d1_run(
            "INSERT OR REPLACE INTO quizzes "
            "(id, name, description, timer, shuffle, csv_data, tag, exp_footer, created_by) "
            "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            [
                quiz_id,
                name,
                f"Poll extract — {len(questions)} প্রশ্ন",
                30,          # default timer 30s
                0,           # no shuffle
                json.dumps(questions),
                "",
                "",
                uid,
            ]
        )
        return quiz_id
    except Exception as e:
        logger.error(f"[poll_extract] D1 save error: {e}")
        return None


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

    ch1, start_id = parse_tg_link(links[0])
    ch2, end_id   = parse_tg_link(links[1])

    if not ch1 or not start_id or not end_id:
        await send_msg(chat_id, "❌ Link parse হয়নি। সঠিক Telegram link দাও।")
        return

    if ch1 != ch2:
        await send_msg(chat_id, "❌ দুটো link একই channel এর হতে হবে!")
        return

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
        f"⏳ Scan করছি: {start_id} → {end_id} ({total} messages)...",
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
        polls = await extract_polls_telethon(ch1, start_id, end_id, progress_cb=progress)
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

    # Save to D1 → get permanent quiz link
    quiz_name = f"Poll Extract [{ch_str} {start_id}-{end_id}]"
    quiz_id   = await save_quiz_to_d1(polls, quiz_name, uid)

    bot_info     = await tg_post("getMe", {})
    bot_username = bot_info.get("result", {}).get("username", "atlasQuizProBot")
    quiz_link    = f"https://t.me/{bot_username}?start={quiz_id}" if quiz_id else None

    # Caption
    caption = (
        f"✅ <b>Poll Extract সম্পন্ন!</b>\n"
        f"📌 Range: {start_id} → {end_id}\n"
        f"📋 Poll পেয়েছি: <b>{len(polls)}</b>\n"
    )
    if quiz_link:
        caption += f"\n🔗 <b>Permanent Quiz Link:</b>\n{quiz_link}"

    await send_document(
        chat_id, csv_bytes, filename,
        caption=caption,
        mime_type="text/csv"
    )
