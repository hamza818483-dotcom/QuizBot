# ============================================================
# ATLAS BOT — D1 Quiz System (quiz.py)
# Fully independent quiz engine backed by Cloudflare D1.
# Created via /q (CSV upload) → shareable quiz link → multi-question
# Telegram poll-based quiz with leaderboard, history, mistake practice.
#
# This system is TOTALLY SEPARATE from the legacy image/text/pdf →
# MCQ → "Quiz Solve" poll system that lives in app.py (QUIZ_STATE).
# Do not merge them.
# ============================================================

import asyncio
import json
import time
import random
from datetime import datetime
import pytz

from core import (
    logger, sb, OWNER_ID,
    d1_set, d1_get, d1_del, d1_query, d1_select, d1_run,
    tg_post, send_msg, edit_msg, send_photo_by_id, send_poll,
    download_tg_file, db_get_settings,
)

# ============================================================
# D1 QUIZ SESSION STATE (in-memory for active D1 quiz play)
# Separate from legacy QUIZ_STATE (image/csv poll quiz) in app.py
# ============================================================
QUIZ_SESSIONS = {}  # uid -> quiz session dict
QUIZ_TIMERS = {}    # uid -> asyncio.Task


def _parse_csv_bytes_local(csv_bytes: bytes) -> list:
    """Lazy import to avoid circular dependency with app.py's CSV parser."""
    from app import _parse_csv_bytes
    return _parse_csv_bytes(csv_bytes)


def _gen_session_id_local() -> str:
    from pdf_handler import gen_session_id
    return gen_session_id()

# ============================================================
# QUIZ CREATE / LIST / DELETE
# ============================================================
async def handle_quiz_create(msg: dict):
    """CSV reply করে /q Name\nDescription\nTimer\nShuffle"""
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text", "")
    reply = msg.get("reply_to_message")

    if not reply or not reply.get("document"):
        await send_msg(chat_id,
            "❌ CSV ফাইলে reply করে <code>/q</code> দাও!\n\n"
            "📝 Format:\n<code>/q Quiz Name\nDescription\nTimer(sec)\nShuffle(Yes/No)</code>"
        )
        return

    lines = text.split("/q", 1)[1].strip().split("\n") if "/q" in text else []
    lines = [l.strip() for l in lines if l.strip()]
    if len(lines) < 4:
        await send_msg(chat_id,
            "❌ ৪টা info দাও:\n1. Name\n2. Description\n3. Timer(sec)\n4. Shuffle(Yes/No)")
        return

    name = lines[0]
    desc = lines[1]
    timer = int(lines[2]) if lines[2].isdigit() else 15
    shuffle = lines[3].lower() == "yes"

    loading = await send_msg(chat_id, "⏳ CSV পড়া হচ্ছে...")

    try:
        csv_bytes = await download_tg_file(reply["document"]["file_id"])
        mcqs = _parse_csv_bytes_local(csv_bytes)
        if not mcqs:
            await send_msg(chat_id, "❌ CSV-তে কোনো MCQ পাওয়া যায়নি!")
            return

        quiz_id = "qz_" + _gen_session_id_local()[:8]
        settings = await db_get_settings()
        tag = settings.get("tag", "")
        exp = settings.get("exp_footer", "")

        questions = []
        for mcq in mcqs:
            ans_map = {"A": 0, "B": 1, "C": 2, "D": 3}
            questions.append({
                "question": mcq["question"],
                "options": mcq["options"],
                "answer_index": ans_map.get(mcq.get("answer", "A"), 0),
                "explanation": mcq.get("explanation", "")
            })

        await d1_run(
            "INSERT OR REPLACE INTO quizzes (id, name, description, timer, shuffle, csv_data, tag, exp_footer, created_by) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            [quiz_id, name, desc, timer, 1 if shuffle else 0, json.dumps(questions), tag, exp, uid]
        )

        bot_info = await tg_post("getMe", {})
        bot_username = bot_info.get("result", {}).get("username", "atlasQuizProBot")
        link = f"https://t.me/{bot_username}?start={quiz_id}"
        web_link = f"https://hamza818483-dotcom.github.io/QuizBot/exam.html?id={quiz_id}"

        await send_msg(chat_id,
            f"✅ <b>Quiz Created!</b>\n\n"
            f"📝 Name: {name}\n📄 Description: {desc}\n"
            f"⏱️ Timer: {timer}s\n🔀 Shuffle: {'Yes' if shuffle else 'No'}\n"
            f"📊 Questions: {len(questions)}\n\n"
            f"🌐 Web Quiz:\n{web_link}\n\n"
            f"🤖 Bot Quiz:\n{link}\n\n"
            f"👆 যে কেউ এই লিংকে ক্লিক করে কুইজ solve করতে পারবে!"
        )
    except Exception as e:
        logger.error(f"[Q] Error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")


async def handle_qlist(msg: dict):
    chat_id = msg["chat"]["id"]
    quizzes = await d1_select("SELECT id, name FROM quizzes ORDER BY created_at ASC")
    if not quizzes:
        await send_msg(chat_id, "❌ কোনো quiz নেই!")
        return
    bot_info = await tg_post("getMe", {})
    bot_username = bot_info.get("result", {}).get("username", "atlasQuizProBot")
    txt = "📋 <b>All Quizzes</b>\n\n"
    for q in quizzes:
        txt += f"📝 {q['name']}\n🔗 https://t.me/{bot_username}?start={q['id']}\n\n"
    await send_msg(chat_id, txt)


async def handle_qdel(msg: dict):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    qid = text.replace("/qdel", "").strip()
    if not qid:
        await send_msg(chat_id, "❌ Usage: /qdel qz_xxx")
        return
    await d1_run("DELETE FROM quizzes WHERE id=?1", [qid])
    await d1_run("DELETE FROM quiz_results WHERE quiz_id=?1", [qid])
    await d1_run("DELETE FROM quiz_leaderboard WHERE quiz_id=?1", [qid])
    await send_msg(chat_id, f"✅ Quiz deleted: {qid}")


async def handle_d1_pre(msg: dict):
    """Quiz Preview Image set/remove"""
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").replace("/pre", "").strip()
    reply = msg.get("reply_to_message")
    if text == "remove":
        await d1_run("DELETE FROM quiz_preview WHERE id=1")
        await send_msg(chat_id, "✅ Preview Image removed!")
    elif reply and reply.get("photo"):
        file_id = reply["photo"][-1]["file_id"]
        await d1_run("INSERT OR REPLACE INTO quiz_preview (id, file_id) VALUES (1, ?1)", [file_id])
        await send_msg(chat_id, "✅ Quiz Preview Image set!")
    else:
        rows = await d1_select("SELECT file_id FROM quiz_preview WHERE id=1")
        if rows and rows[0].get("file_id"):
            await send_msg(chat_id, "🖼️ Preview is set.\n/pre remove to delete")
        else:
            await send_msg(chat_id, "❌ No preview!\nReply to image with /pre")


async def handle_d1_info(msg: dict):
    """D1 quiz system stats"""
    chat_id = msg["chat"]["id"]
    users = await d1_select("SELECT COUNT(*) as c FROM bot_users")
    quizzes = await d1_select("SELECT COUNT(*) as c FROM quizzes")
    attempts = await d1_select("SELECT COUNT(*) as c FROM quiz_results")
    top = await d1_select("SELECT user_name, COUNT(*) as c FROM quiz_results GROUP BY user_id ORDER BY c DESC LIMIT 3")

    txt = "📊 <b>D1 Quiz Stats</b>\n\n"
    txt += f"👥 Total Users: {users[0]['c'] if users else 0}\n"
    txt += f"📝 Total Quizzes: {quizzes[0]['c'] if quizzes else 0}\n"
    txt += f"🎯 Total Attempts: {attempts[0]['c'] if attempts else 0}\n"
    medals = ["🥇", "🥈", "🥉"]
    if top:
        txt += "\n🔝 Top Quiz Solvers:\n"
        for i, r in enumerate(top):
            txt += f"{medals[i] if i < 3 else ''} {r['user_name']} — {r['c']} quizzes\n"
    await send_msg(chat_id, txt)


async def handle_d1_send(msg: dict):
    """Broadcast message to all quiz bot users"""
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    reply = msg.get("reply_to_message")
    if not reply:
        await send_msg(chat_id, "❌ Reply to a message with /send")
        return
    reply_msg_id = reply["message_id"]
    users = await d1_select("SELECT user_id FROM bot_users")
    channels = await d1_select("SELECT chat_id FROM channels")
    total_users = len(users)
    total_chs = len(channels)

    kb = {"inline_keyboard": [
        [{"text": f"👥 All Users ({total_users})", "callback_data": f"d1send_users_{uid}"}],
        [{"text": f"📢 All Channels ({total_chs})", "callback_data": f"d1send_chns_{uid}"}],
        [{"text": f"👥+📢 Both ({total_users + total_chs})", "callback_data": f"d1send_both_{uid}"}]
    ]}

    sb.table("quiz_sessions").upsert({
        "key": f"d1_send_{uid}",
        "data": json.dumps({"msg_id": reply_msg_id, "chat_id": chat_id}),
        "updated_at": int(time.time())
    }).execute()

    await send_msg(chat_id,
        f"📤 Send This Message To:\n\n"
        f"👥 Total Users: {total_users}\n📢 Total Channels: {total_chs}",
        reply_markup=kb
    )


async def handle_d1_send_cb(query: dict):
    """Broadcast callback handler"""
    data = query["data"]
    chat_id = query["message"]["chat"]["id"]
    uid = query["from"]["id"]

    parts = data.split("_")
    target = parts[1]  # users/chns/both
    orig_uid = int(parts[2])
    if uid != orig_uid:
        return

    row = sb.table("quiz_sessions").select("data").eq("key", f"d1_send_{uid}").execute()
    if not row.data:
        return
    info = json.loads(row.data[0]["data"])
    msg_id = info["msg_id"]
    from_chat_id = info["chat_id"]

    sent = 0
    if target in ("users", "both"):
        users = await d1_select("SELECT user_id FROM bot_users")
        for u in users:
            try:
                await tg_post("forwardMessage", {
                    "chat_id": u["user_id"], "from_chat_id": from_chat_id, "message_id": msg_id
                })
                sent += 1
                await asyncio.sleep(0.05)
            except:
                pass
    if target in ("chns", "both"):
        channels = await d1_select("SELECT chat_id FROM channels")
        for ch in channels:
            try:
                await tg_post("forwardMessage", {
                    "chat_id": ch["chat_id"], "from_chat_id": from_chat_id, "message_id": msg_id
                })
                sent += 1
                await asyncio.sleep(0.05)
            except:
                pass

    sb.table("quiz_sessions").delete().eq("key", f"d1_send_{uid}").execute()
    await send_msg(chat_id, f"✅ Sent to {sent} recipients!")


async def start_d1_quiz(chat_id: int, quiz_id: str, user: dict, mistake_qs=None, mistake_type=None):
    """Start a quiz from D1 database"""
    uid = user["id"]
    uname = user.get("first_name", "Student")

    if mistake_qs:
        questions = mistake_qs
        quiz = {"timer": 15, "tag": "", "exp_footer": "", "name": "Practice", "description": ""}
        row = sb.table("quiz_sessions").select("data").eq("key", f"d1_otag_{uid}").execute()
        if row.data:
            orig = json.loads(row.data[0]["data"])
            quiz["timer"] = orig.get("timer", 15)
            quiz["tag"] = orig.get("tag", "")
            quiz["exp_footer"] = orig.get("exp", "")
            quiz["name"] = orig.get("name", "Practice")
    else:
        rows = await d1_select("SELECT * FROM quizzes WHERE id=?1", [quiz_id])
        if not rows:
            # Supabase backup থেকে restore
            try:
                import httpx as _hx, os as _os
                _sb_url = _os.environ.get("SUPABASE_URL","")
                _sb_key = _os.environ.get("SUPABASE_KEY","")
                if _sb_url and _sb_key:
                    async with _hx.AsyncClient(timeout=10) as _c:
                        _r = await _c.get(f"{_sb_url}/rest/v1/quiz_backups",
                            headers={"apikey":_sb_key,"Authorization":f"Bearer {_sb_key}"},
                            params={"quiz_id":f"eq.{quiz_id}","select":"*"})
                    _b = _r.json()
                    if _b and _b[0]:
                        _bk = _b[0]
                        await d1_run(
                            "INSERT OR REPLACE INTO quizzes (id,name,description,timer,shuffle,csv_data,tag,exp_footer,created_by) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                            [quiz_id,_bk["name"],"",30,0,json.dumps(_bk["questions"]),"","",0]
                        )
                        rows = await d1_select("SELECT * FROM quizzes WHERE id=?1",[quiz_id])
            except Exception as _e:
                logger.warning(f"[quiz] Supabase restore: {_e}")
        if not rows:
            await send_msg(chat_id, "❌ কুইজ পাওয়া যায়নি! Link টা সঠিক কিনা দেখো।")
            return
        quiz = rows[0]
        questions = json.loads(quiz["csv_data"])
        if quiz.get("shuffle"):
            import copy
            questions = copy.deepcopy(questions)
            random.shuffle(questions)
            for q in questions:
                correct_opt = q["options"][q["answer_index"]]
                random.shuffle(q["options"])
                q["answer_index"] = q["options"].index(correct_opt)

        sb.table("quiz_sessions").upsert({
            "key": f"d1_otag_{uid}",
            "data": json.dumps({
                "timer": quiz.get("timer", 15), "tag": quiz.get("tag", ""),
                "exp": quiz.get("exp_footer", ""), "name": quiz.get("name", "")
            }),
            "updated_at": int(time.time())
        }).execute()

    session = {
        "quiz_id": (quiz_id + "mp") if mistake_qs else quiz_id,
        "name": quiz.get("name", "Quiz") + (" — Practice" if mistake_qs else ""),
        "desc": quiz.get("description", ""),
        "questions": questions,
        "cur": 0,
        "tot": len(questions),
        "right": 0,
        "wrong": 0,
        "skip": 0,
        "timer": quiz.get("timer", 15) if isinstance(quiz.get("timer"), int) else int(quiz.get("timer", 15)),
        "tag": quiz.get("tag", ""),
        "exp": quiz.get("exp_footer", ""),
        "chat_id": chat_id,
        "uname": uname,
        "uid": uid,
        "pid": None,
        "cor": None,
        "q_results": [],
        "is_mistake": bool(mistake_qs)
    }

    QUIZ_SESSIONS[uid] = session

    if mistake_qs:
        intro = f"📝 {session['name']}\n"
        if mistake_type == "wrong":
            intro += f"❌ Wrong Questions: {len(questions)}\n"
        else:
            intro += f"❌ Wrong+Skip: {len(questions)}\n"
        intro += "🔄 Practice\n\nএখনই কুইজ আসবে, আপনি প্রস্তুত তো? 😎"
        await send_msg(chat_id, intro)
    else:
        preview = await d1_select("SELECT file_id FROM quiz_preview WHERE id=1")
        info_text = (
            f"📝 {session['name']}\n📄 {session['desc']}\n"
            f"⏱️ Timer: {session['timer']}s\n📊 Questions: {session['tot']}"
        )
        if preview and preview[0].get("file_id"):
            await send_photo_by_id(chat_id, preview[0]["file_id"], info_text)
        else:
            await send_msg(chat_id, info_text)

    for cd in ["3...", "2...", "1..."]:
        await asyncio.sleep(0.7)
        await send_msg(chat_id, cd)
    await asyncio.sleep(1)
    await send_quiz_question(chat_id, session)


async def send_quiz_question(chat_id: int, session: dict):
    """Send the current quiz question as a poll"""
    if session["cur"] >= session["tot"]:
        await finish_d1_quiz(session)
        return

    q = session["questions"][session["cur"]]
    session["q_results"].append({"index": session["cur"], "type": None})

    tag_part = f"{session['tag']}\n\n" if session["tag"] else ""
    q_text = f"{tag_part}{session['cur'] + 1}. {q.get('question', '?')}"[:300]
    exp = q.get("explanation", "")
    if session["exp"]:
        exp = f"{exp}\n{session['exp']}"
    exp = exp[:200]

    opts = q.get("options", [])
    ans_idx = q.get("answer_index", 0)
    if len(opts) > 4:
        # keep correct answer in range — swap it into slot 4 (index 3) before trimming
        if ans_idx >= 4:
            opts = opts[:3] + [opts[ans_idx]]
            ans_idx = 3
        else:
            opts = opts[:4]

    poll_r = await send_poll(
        chat_id, q_text, [o[:100] for o in opts], ans_idx,
        explanation=exp, is_anonymous=False, open_period=session["timer"]
    )

    if poll_r.get("ok"):
        poll_id = poll_r["result"].get("poll", {}).get("id", "")
        session["pid"] = poll_id
        session["cor"] = ans_idx
        QUIZ_SESSIONS[session["uid"]] = session

        # Timer: auto-skip after timer expires
        async def _quiz_timeout():
            await asyncio.sleep(session["timer"] + 2)
            s = QUIZ_SESSIONS.get(session["uid"])
            if not s or s["pid"] != poll_id or s["cur"] != session["cur"]:
                return
            # Auto-advance — skip হিসেবে count করো
            for qr in s["q_results"]:
                if qr["index"] == s["cur"] and qr["type"] == "pending":
                    qr["type"] = "skip"
                    break
            s["skip"] += 1
            s["cur"] += 1
            QUIZ_SESSIONS[s["uid"]] = s
            # 1s gap দিয়ে next question auto-send
            await asyncio.sleep(1)
            await send_quiz_question(chat_id, s)
        if session["uid"] in QUIZ_TIMERS:
            QUIZ_TIMERS[session["uid"]].cancel()
        QUIZ_TIMERS[session["uid"]] = asyncio.create_task(_quiz_timeout())


async def handle_quiz_poll_answer(pa: dict):
    """Handle poll answer for D1 quiz system"""
    uid = pa.get("user", {}).get("id")
    if not uid or uid not in QUIZ_SESSIONS:
        return

    session = QUIZ_SESSIONS[uid]
    poll_id = pa.get("poll_id", "")
    if session.get("pid") != poll_id:
        return

    option_ids = pa.get("option_ids", [])
    q_result = None
    for qr in session["q_results"]:
        if qr["index"] == session["cur"]:
            q_result = qr
            break

    if q_result:
        if not option_ids:
            q_result["type"] = "skip"
        elif option_ids[0] == session["cor"]:
            q_result["type"] = "right"
        else:
            q_result["type"] = "wrong"

    if not option_ids:
        session["skip"] += 1
    elif option_ids[0] == session["cor"]:
        session["right"] += 1
    else:
        session["wrong"] += 1

    session["cur"] += 1

    if uid in QUIZ_TIMERS:
        QUIZ_TIMERS[uid].cancel()

    if session["cur"] >= session["tot"]:
        await finish_d1_quiz(session)
    else:
        await send_quiz_question(session["chat_id"], session)


async def handle_quiz_next(uid: int):
    """Handle Next button click — skip question"""
    session = QUIZ_SESSIONS.get(uid)
    if not session:
        return

    # টাইমআউট মেসেজ (⏱️ সময় শেষ! + Next button) মুছে ফেলো
    if session.get("timeout_msg_id"):
        await tg_post("deleteMessage", {
            "chat_id": session["chat_id"],
            "message_id": session["timeout_msg_id"]
        })
        session["timeout_msg_id"] = None

    q_result = None
    for qr in session["q_results"]:
        if qr["index"] == session["cur"]:
            q_result = qr
            break
    if q_result:
        q_result["type"] = "skip"

    session["skip"] += 1
    session["cur"] += 1

    if uid in QUIZ_TIMERS:
        QUIZ_TIMERS[uid].cancel()

    if session["cur"] >= session["tot"]:
        await finish_d1_quiz(session)
    else:
        await send_quiz_question(session["chat_id"], session)


async def finish_d1_quiz(session: dict):
    """Quiz finish — show results, save to D1"""
    uid = session["uid"]
    chat_id = session["chat_id"]
    QUIZ_SESSIONS.pop(uid, None)
    QUIZ_TIMERS.pop(uid, None)

    tot = session["tot"]
    right = session["right"]
    wrong = session["wrong"]
    skip = session["skip"]
    name = session["name"]
    uname = session["uname"]
    quiz_id = session["quiz_id"]
    score = f"{right}/{tot}"
    pct = round(right / tot * 100) if tot else 0

    # Save result to D1
    try:
        cnt = await d1_select(
            "SELECT COUNT(*) as cnt FROM quiz_results WHERE user_id=?1 AND quiz_id=?2",
            [uid, quiz_id]
        )
        attempt = (cnt[0]["cnt"] if cnt else 0) + 1

        # RELIABILITY: this INSERT is the one thing that makes "Practice
        # (Wrong only/Wrong+Skip)" work later — a single transient network
        # hiccup here used to permanently break mistake-practice for this
        # attempt with zero visibility. Retry a couple of times before
        # giving up, since the summary message gets sent either way.
        _ok = False
        result_id = None
        for _attempt_n in range(3):
            _ok, result_id = await d1_run(
                "INSERT INTO quiz_results (user_id, user_name, quiz_id, right_count, wrong_count, skip_count, total, score, attempt) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                [uid, uname, quiz_id, right, wrong, skip, tot, score, attempt],
                return_id=True
            )
            if _ok:
                break
            logger.warning(f"[Quiz] quiz_results insert attempt {_attempt_n+1}/3 failed, retrying...")
            await asyncio.sleep(0.6 * (_attempt_n + 1))
        if not _ok:
            logger.error(f"[Quiz] quiz_results insert FAILED after 3 attempts for user={uid} quiz={quiz_id} — mistake-practice will show 'No previous attempt found' for this run")

        # Fallback: if meta.last_row_id unavailable, look it up via a fresh SELECT
        if not result_id:
            rid_rows = await d1_select(
                "SELECT id FROM quiz_results WHERE user_id=?1 AND quiz_id=?2 AND attempt=?3 ORDER BY id DESC LIMIT 1",
                [uid, quiz_id, attempt]
            )
            result_id = rid_rows[0]["id"] if rid_rows else None

        # Save per-question results
        if result_id:
            for qr in session.get("q_results", []):
                if qr.get("type"):
                    await d1_run(
                        "INSERT INTO quiz_question_results (result_id, question_index, result_type, quiz_id, user_id) VALUES (?1, ?2, ?3, ?4, ?5)",
                        [result_id, qr["index"], qr["type"], quiz_id, uid]
                    )

        # Update leaderboard
        existing = await d1_select(
            "SELECT right_count FROM quiz_leaderboard WHERE quiz_id=?1 AND user_id=?2",
            [quiz_id, uid]
        )
        if existing:
            if right > existing[0]["right_count"]:
                await d1_run(
                    "UPDATE quiz_leaderboard SET user_name=?1, score=?2, right_count=?3, total=?4, updated_at=?5 WHERE quiz_id=?6 AND user_id=?7",
                    [uname, score, right, tot, int(time.time()), quiz_id, uid]
                )
        else:
            await d1_run(
                "INSERT INTO quiz_leaderboard (quiz_id, user_id, user_name, score, right_count, total, updated_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                [quiz_id, uid, uname, score, right, tot, int(time.time())]
            )
    except Exception as e:
        logger.error(f"[Quiz] Save result error: {e}")

    # Motivation
    if pct >= 90:
        mot = "🏆 অসাধারণ! তুমি সেরা!"
    elif pct >= 70:
        mot = "🎉 চমৎকার! আরও প্র্যাকটিস করো!"
    elif pct >= 50:
        mot = "👍 মোটামুটি ভালো! আরও পড়ো!"
    else:
        mot = "📚 পড়া হয়নি! আবার চেষ্টা করো!"

    original_qid = quiz_id.replace("mp", "") if session.get("is_mistake") else quiz_id
    bot_info = await tg_post("getMe", {})
    bot_username = bot_info.get("result", {}).get("username", "atlasQuizProBot")
    link = f"https://t.me/{bot_username}?start={original_qid}"

    txt = (
        f"🌟 {name} কুইজে অংশগ্রহণের জন্য অভিনন্দন, {uname}!\n\n"
        f"📊 তোমার রেজাল্ট:\n"
        f"✅ Right: {right}\n❌ Wrong: {wrong}\n😐 Skipped: {skip}\n\n"
        f"⚡ Final: {score} ({pct}%)\n\n{mot}"
    )

    if session.get("is_mistake"):
        kb = {"inline_keyboard": [[{"text": "📌 আবার প্রাক্টিস করো", "url": link}]]}
    else:
        kb_rows = [
            [{"text": "📌 আবার প্রাক্টিস করো", "url": link}],
            [{"text": "👥 Leaderboard", "callback_data": f"qzlb_{quiz_id}"},
             {"text": "📈 History", "callback_data": f"qzhist_{quiz_id}"}],
        ]
        if wrong > 0:
            kb_rows.append([{"text": f"🔴 Practice (Wrong only) ({wrong})", "callback_data": f"qzmp1_{quiz_id}"}])
        if (wrong + skip) > 0:
            kb_rows.append([{"text": f"🟡 Practice (Wrong+Skip) ({wrong + skip})", "callback_data": f"qzmp2_{quiz_id}"}])
        kb = {"inline_keyboard": kb_rows}

    await send_msg(chat_id, txt, reply_markup=kb)


async def handle_d1_leaderboard(chat_id: int, quiz_id: str, uid: int):
    lb = await d1_select(
        "SELECT user_name, score, right_count, total, user_id FROM quiz_leaderboard WHERE quiz_id=?1 ORDER BY right_count DESC",
        [quiz_id]
    )
    if not lb:
        await send_msg(chat_id, "🏆 এখনো কেউ quiz solve করেনি!")
        return

    your_pos = -1
    for i, r in enumerate(lb):
        if r["user_id"] == uid:
            your_pos = i + 1

    txt = ""
    if your_pos > 0:
        txt += f"📊 Your Position: #{your_pos}\n\n"
    txt += "🏆 <b>Leaderboard</b>\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(lb):
        medal = medals[i] if i < 3 else f"{i+1}."
        is_you = r["user_id"] == uid
        p = round(r["right_count"] / r["total"] * 100) if r["total"] else 0
        txt += f"{'<b>' if is_you else ''}{medal} {r['user_name']} — {r['score']} ({p}%)"
        if is_you:
            txt += " 👈 You"
        txt += f"{'</b>' if is_you else ''}\n"
    await send_msg(chat_id, txt)


async def handle_d1_history(chat_id: int, quiz_id: str, uid: int):
    hist = await d1_select(
        "SELECT score, attempt, created_at FROM quiz_results WHERE user_id=?1 AND quiz_id=?2 ORDER BY attempt",
        [uid, quiz_id]
    )
    txt = "📈 <b>Progress</b>\n\n"
    if hist:
        for r in hist:
            txt += f"🟢 Attempt {r['attempt']}: {r['score']}"
            if r.get("created_at"):
                from datetime import datetime as dt
                txt += f" | 📅 {dt.fromtimestamp(r['created_at']).strftime('%Y-%m-%d')}"
            txt += "\n"
    else:
        txt += "এখনো কোনো history নেই!"
    await send_msg(chat_id, txt)


async def handle_d1_mistake(chat_id: int, quiz_id: str, uid: int, user: dict, mtype: str):
    """Mistake practice — replay wrong/skipped questions"""
    try:
        last = await d1_select(
            "SELECT id FROM quiz_results WHERE user_id=?1 AND quiz_id=?2 ORDER BY id DESC LIMIT 1",
            [uid, quiz_id]
        )
        if not last:
            await send_msg(chat_id, "❌ No previous attempt found!")
            return

        result_id = last[0]["id"]
        if mtype == "wrong":
            wrong_qs = await d1_select(
                "SELECT question_index FROM quiz_question_results WHERE result_id=?1 AND result_type='wrong'",
                [result_id]
            )
        else:
            wrong_qs = await d1_select(
                "SELECT question_index FROM quiz_question_results WHERE result_id=?1 AND result_type IN ('wrong', 'skip')",
                [result_id]
            )

        if not wrong_qs:
            if mtype == "wrong":
                await send_msg(chat_id, "🎉 সব সঠিক ছিল! Practice-এর প্রয়োজন নেই!")
            else:
                await send_msg(chat_id, "🎉 সব সঠিক ছিল, skip-ও নেই!")
            return

        quiz_rows = await d1_select("SELECT * FROM quizzes WHERE id=?1", [quiz_id])
        if not quiz_rows:
            await send_msg(chat_id, "❌ Quiz not found!")
            return

        all_questions = json.loads(quiz_rows[0]["csv_data"])
        practice = [all_questions[r["question_index"]] for r in wrong_qs
                     if r["question_index"] < len(all_questions)]
        if not practice:
            await send_msg(chat_id, "❌ Questions not found!")
            return

        await start_d1_quiz(chat_id, quiz_id, user, mistake_qs=practice, mistake_type=mtype)
    except Exception as e:
        logger.error(f"[Mistake] Error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")

async def create_quiz_from_mcqs(mcqs: list, name: str, uid: int) -> str:
    """MCQ list → D1 quiz save → quiz_id return"""
    from pdf_handler import gen_session_id
    ANS_MAP = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
    questions = []
    for q in mcqs:
        questions.append({
            "question": q.get("question", ""),
            "options": q.get("options", []),
            "answer_index": ANS_MAP.get(q.get("answer", "A"), 0),
            "explanation": q.get("explanation", ""),
        })
    quiz_id = "qz_" + gen_session_id()[:8]
    await d1_run(
        "INSERT OR REPLACE INTO quizzes (id,name,description,timer,shuffle,csv_data,tag,exp_footer,created_by) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
        [quiz_id, name, f"{len(questions)} প্রশ্ন", 30, 0, json.dumps(questions), "", "", uid]
    )
    return quiz_id
