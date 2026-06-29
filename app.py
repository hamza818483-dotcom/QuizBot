# ============================================================
# ATLAS BOT — Main App (HF Space)
# FastAPI + Telegram Bot + PDF MCQ System
# v4.1 — Live Quiz Update (June 2026)
#         + /start /help with full command list (admin/user split)
#         + Live Quiz: View Votes button, instant next, option tracking
#         + Live Quiz: 15% qualification threshold (was 30%)
#         + /img: inline buttons (Quiz Solve, Poll Solve, Web Exam)
#         + /setcommand: register all bot commands
#         + multi-AI vision rotation preserved
#         all v4.0 features preserved
# ============================================================

import os
import json
import logging
import asyncio
import time
import random
import string
import re
import base64
from io import BytesIO
from typing import Optional
from datetime import datetime
from datetime import timedelta
import pytz
import html as html_lib

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from supabase.client import Client

from pdf_handler import (
    pdf_to_images, image_to_bytes, generate_mcq_from_image,
    generate_new_mcq, parse_pdf_command, parse_page_range,
    fmt_page, gen_session_id, get_random_ayat, get_motivation,
    key_rotator
)

from core import (
    logger, app, sb,
    BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY, OWNER_ID,
    CF_WORKER_URL, HF_SPACE_URL, D1_TOKEN, TG_API,
    d1_set, d1_get, d1_del, d1_query, d1_select, d1_run,
    tg_post, send_msg, edit_msg, send_photo, send_photo_by_id,
    send_document, send_poll, notify_owner, download_tg_file,
    db_get_settings, db_is_owner_or_admin, db_track_user, db_save_session,
    db_save_mcq_cache, db_update_cache, db_get_mcq_cache,
    db_get_new_gen_count, db_increment_gen_count, db_save_leaderboard,
    db_get_channels, db_save_last_quiz, db_get_last_quiz,
    build_back_url, source_msg_id,
    get_recent_errors, clear_error_logs,
    add_watermark_to_pdf,
)

# chorcha.net mhtml/html → Premium PDF (Question Bank converter)
from chorcha_parser import parse_chorcha_file
from chorcha_pdf import build_chorcha_pdf_html

# D1 Quiz System (fully independent module — see quiz.py)
from quiz import (
    QUIZ_SESSIONS, QUIZ_TIMERS,
    handle_quiz_create, handle_qlist, handle_qdel,
    handle_d1_pre, handle_d1_info, handle_d1_send, handle_d1_send_cb,
    start_d1_quiz, send_quiz_question as send_d1_quiz_question,
    handle_quiz_poll_answer, handle_quiz_next, finish_d1_quiz,
    handle_d1_leaderboard, handle_d1_history, handle_d1_mistake,
)

# ============================================================
# APP-LOCAL CONFIG (not shared with quiz.py)
# ============================================================
# PIN SYSTEM
PIN_ENABLED = {}  # chat_id -> bool (in-memory, also saved to DB)

# LIVE QUIZ CONFIG
LIVE_QUIZ_STATE = {}  # channel_id -> live quiz state
LIVE_TIMERS = {}      # channel_id -> timer task

# IMAGE COLLECTION (for /pdf image→PDF feature)
IMG_COLLECTION = {}   # uid -> {"imgs": [], "collecting": bool}

# v1.2: /watermark feature — uid -> pdf_bytes (waiting for watermark text)
WATERMARK_PENDING = {}


# v1.3: /rapid — scheduled comment-based question drop in a channel
# uid -> {"step": "awaiting_time", "topic":..., "mcqs":..., "channel_id":...}
RAPID_PENDING = {}
# job_id -> asyncio.Task (so a scheduled /rapid run can be cancelled before it fires)
RAPID_TASKS = {}

# v1.3: /api/new-exam — async job state for instant progress page
# job_id -> {"status": "running"|"done"|"error", "pct": int, "eta_sec": int,
#            "started_at": float, "new_cache_id": str, "error": str}
NEW_EXAM_JOBS = {}

# v1.2: /ping status command — set at startup, used to compute uptime
BOT_START_TIME = time.time()

# DEFAULT LIVE QUIZ TIME (seconds per question)
DEFAULT_LIVE_TIME = 10

# ============================================================
# MULTI-AI MODEL ROTATION (Vision MCQ generation)
# Order: Gemini (via pdf_handler) → NVIDIA Llama 3.2 11B Vision
#        → OpenRouter Qwen2-VL 72B → Nemotron Nano Omni → Gemma
# Missing keys are skipped silently — never raise.
# ============================================================
import base64 as _b64_ai
from pdf_handler import generate_mcq_from_image as _gemini_gen_mcq

_AI_PROVIDERS_ORDER = ["nvidia", "openrouter_qwen", "nemotron", "gemma"]

def _img_to_data_url(img) -> str:
    try:
        buf = BytesIO()
        if hasattr(img, "save"):
            img.save(buf, format="JPEG", quality=85)
            data = buf.getvalue()
        elif isinstance(img, (bytes, bytearray)):
            data = bytes(img)
        else:
            data = bytes(img)
        return "data:image/jpeg;base64," + _b64_ai.b64encode(data).decode()
    except Exception:
        return ""

def _build_mcq_prompt(topic: str, count) -> str:
    n_txt = f"{count}" if count else "যতগুলো প্রশ্ন/MCQ ছবিতে আছে সব"
    return (
        f"You are an MCQ extraction expert for Bengali/English academic content.\n"
        f"Topic: {topic}\n"
        f"From the given page image, extract {n_txt} MCQs.\n"
        f"STRICT LANGUAGE RULE: Detect the language of the source image text "
        f"(Bengali or English) and write the question, ALL options, and the "
        f"explanation in that exact same language. Never translate — if the "
        f"source is English, output English; if the source is Bengali, output "
        f"Bengali.\n"
        f"Return STRICT JSON array only, no prose, no markdown fences. Schema:\n"
        f"[{{\"question\":\"...\",\"options\":[\"A\",\"B\",\"C\",\"D\"],"
        f"\"answer\":\"A|B|C|D\",\"explanation\":\"...\"}}]"
    )

def _parse_mcq_json(text: str) -> list:
    if not text:
        return []
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    # try to locate first '[' .. last ']'
    a = s.find("[")
    b = s.rfind("]")
    if a != -1 and b != -1 and b > a:
        s = s[a:b+1]
    try:
        data = json.loads(s)
    except Exception:
        return []
    out = []
    if isinstance(data, list):
        for it in data:
            if not isinstance(it, dict):
                continue
            q = (it.get("question") or it.get("q") or "").strip()
            opts = it.get("options") or it.get("opts") or []
            if not q or not isinstance(opts, list) or len(opts) < 2:
                continue
            opts = [str(o)[:300] for o in opts][:4]
            ans = str(it.get("answer", "A")).strip().upper()
            if ans in ("1","2","3","4"):
                ans = {"1":"A","2":"B","3":"C","4":"D"}[ans]
            if ans not in ("A","B","C","D"):
                ans = "A"
            out.append({
                "question": q,
                "options": opts,
                "answer": ans,
                "explanation": str(it.get("explanation",""))[:500],
            })
    return out

async def _post_openai_compat(url: str, key: str, model: str, data_url: str, prompt: str) -> str:
    if not key:
        return ""
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}}
            ]
        }],
        "temperature": 0.3,
        "max_tokens": 4096,
    }
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(url, headers=headers, json=payload)
            if r.status_code >= 400:
                logger.warning(f"[AI-ROT] {model} HTTP {r.status_code}: {r.text[:200]}")
                return ""
            j = r.json()
            return j.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    except Exception as e:
        logger.warning(f"[AI-ROT] {model} err: {e}")
        return ""

async def _gen_nvidia(img, topic, count):
    key = os.environ.get("NVIDIA_API_KEY", "")
    if not key:
        return []
    data_url = _img_to_data_url(img)
    if not data_url:
        return []
    txt = await _post_openai_compat(
        "https://integrate.api.nvidia.com/v1/chat/completions",
        key, "meta/llama-3.2-11b-vision-instruct",
        data_url, _build_mcq_prompt(topic, count)
    )
    return _parse_mcq_json(txt)

async def _gen_openrouter_qwen(img, topic, count):
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return []
    data_url = _img_to_data_url(img)
    if not data_url:
        return []
    txt = await _post_openai_compat(
        "https://openrouter.ai/api/v1/chat/completions",
        key, "qwen/qwen-2-vl-72b-instruct",
        data_url, _build_mcq_prompt(topic, count)
    )
    return _parse_mcq_json(txt)

async def _gen_nemotron(img, topic, count):
    key = os.environ.get("NEMOTRON_API_KEY", "") or os.environ.get("NVIDIA_API_KEY", "")
    if not key:
        return []
    data_url = _img_to_data_url(img)
    if not data_url:
        return []
    txt = await _post_openai_compat(
        "https://integrate.api.nvidia.com/v1/chat/completions",
        key, "nvidia/nemotron-nano-12b-v2-vl",
        data_url, _build_mcq_prompt(topic, count)
    )
    return _parse_mcq_json(txt)

async def _gen_gemma(img, topic, count):
    key = os.environ.get("GEMMA_API_KEY", "") or os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return []
    data_url = _img_to_data_url(img)
    if not data_url:
        return []
    # Gemma 3 27B IT vision via OpenRouter
    txt = await _post_openai_compat(
        "https://openrouter.ai/api/v1/chat/completions",
        key, "google/gemma-3-27b-it",
        data_url, _build_mcq_prompt(topic, count)
    )
    return _parse_mcq_json(txt)

_AI_FALLBACK_FNS = {
    "nvidia":          _gen_nvidia,
    "openrouter_qwen": _gen_openrouter_qwen,
    "nemotron":        _gen_nemotron,
    "gemma":           _gen_gemma,
}

async def generate_mcq_from_image(img, topic, page_num, mcq_count=None):
    """
    Smart wrapper: Gemini first (with internal key rotation via pdf_handler).
    On failure → rotate through NVIDIA / OpenRouter Qwen VL / Nemotron / Gemma.
    Missing API keys are skipped silently. Never raises.
    """
    # 1) Gemini (preferred — healthy key → use it)
    try:
        out = await _gemini_gen_mcq(img, topic, page_num, mcq_count)
        if out:
            return out
        logger.warning(f"[AI-ROT] gemini returned empty (page {page_num}); rotating to fallbacks")
    except Exception as e:
        logger.warning(f"[AI-ROT] gemini failed (page {page_num}): {e}; rotating to fallbacks")

    # 2) Fallback providers (skip silently if key missing / call fails)
    for prov in _AI_PROVIDERS_ORDER:
        fn = _AI_FALLBACK_FNS.get(prov)
        if not fn:
            continue
        try:
            out = await fn(img, topic, mcq_count)
            if out:
                logger.info(f"[AI-ROT] page {page_num} satisfied by provider={prov}")
                return out
        except Exception as e:
            logger.warning(f"[AI-ROT] provider {prov} crashed: {e}")
            continue

    logger.error(f"[AI-ROT] all providers exhausted for page {page_num}")
    return []


# ============================================================
# QUIZ SESSION STATE (in-memory for active quiz play — shared with quiz.py)
# ============================================================
# QUIZ_SESSIONS / QUIZ_TIMERS (D1 quiz in-memory state) now live in quiz.py

DEFAULT_TOPIC = "Pagewise MCQ Solve By ATLAS"
QUIZ_Q_SEC = 35

# ============================================================
# DB HELPERS — PIN SYSTEM
# ============================================================
async def db_get_pin_setting(chat_id) -> bool:
    try:
        r = sb.table("bot_settings").select("value").eq("key", f"pin_{chat_id}").execute()
        if r.data:
            return r.data[0]["value"] == "on"
    except:
        pass
    return False

async def db_set_pin_setting(chat_id, enabled: bool):
    try:
        sb.table("bot_settings").upsert({
            "key": f"pin_{chat_id}",
            "value": "on" if enabled else "off"
        }).execute()
    except Exception as e:
        logger.error(f"[DB] set_pin error: {e}")

async def db_get_live_time(chat_id) -> int:
    try:
        r = sb.table("bot_settings").select("value").eq("key", f"livetime_{chat_id}").execute()
        if r.data:
            return int(r.data[0]["value"])
    except:
        pass
    return DEFAULT_LIVE_TIME

async def db_set_live_time(chat_id, seconds: int):
    try:
        sb.table("bot_settings").upsert({
            "key": f"livetime_{chat_id}",
            "value": str(seconds)
        }).execute()
    except Exception as e:
        logger.error(f"[DB] set_livetime error: {e}")

# ============================================================
# DB HELPERS — OVERFLOW AUTO-DELETE (STEP 9)
# ============================================================
async def db_auto_cleanup_if_needed():
    """
    Supabase বা D1 full হলে সবচেয়ে পুরনো data delete করে।
    প্রতি 100 request-এ একবার check করে।
    """
    try:
        # pdf_mcq_cache — 10000 rows limit রাখো
        r = sb.table("pdf_mcq_cache").select("id", count="exact").execute()
        if (r.count or 0) > 10000:
            old = sb.table("pdf_mcq_cache").select("id")\
                .order("created_at").limit(500).execute()
            ids = [row["id"] for row in (old.data or [])]
            if ids:
                sb.table("pdf_mcq_cache").delete().in_("id", ids).execute()
                logger.info(f"[Cleanup] Deleted {len(ids)} old cache rows")

        # web_exam_results — 50000 rows limit
        r2 = sb.table("web_exam_results").select("id", count="exact").execute()
        if (r2.count or 0) > 50000:
            old2 = sb.table("web_exam_results").select("id")\
                .order("created_at").limit(1000).execute()
            ids2 = [row["id"] for row in (old2.data or [])]
            if ids2:
                sb.table("web_exam_results").delete().in_("id", ids2).execute()
                logger.info(f"[Cleanup] Deleted {len(ids2)} old exam results")

        # pdf_sessions — 5000 rows limit
        r3 = sb.table("pdf_sessions").select("id", count="exact").execute()
        if (r3.count or 0) > 5000:
            old3 = sb.table("pdf_sessions").select("id")\
                .order("created_at").limit(200).execute()
            ids3 = [row["id"] for row in (old3.data or [])]
            if ids3:
                sb.table("pdf_sessions").delete().in_("id", ids3).execute()
    except Exception as e:
        logger.error(f"[Cleanup] Error: {e}")

# ============================================================
# DB HELPERS — LIVE QUIZ RESULTS
# ============================================================
async def db_save_live_result(session_id: str, user_id: int, user_name: str,
                               correct: int, wrong: int, skipped: int,
                               total: int, avg_time: float):
    try:
        sb.table("live_quiz_results").upsert({
            "session_id": session_id,
            "user_id": user_id,
            "user_name": user_name,
            "correct": correct,
            "wrong": wrong,
            "skipped": skipped,
            "total": total,
            "avg_response_time": avg_time,
            "score": correct,
            "updated_at": int(time.time())
        }).execute()
    except Exception as e:
        logger.error(f"[DB] save_live_result error: {e}")

async def db_get_live_results(session_id: str) -> list:
    try:
        r = sb.table("live_quiz_results").select("*")\
            .eq("session_id", session_id)\
            .order("score", desc=True).execute()
        return r.data or []
    except:
        return []

# ============================================================
# FEATURE 1: /start
# ============================================================
async def handle_start(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    uname = msg["from"].get("first_name", "User")
    await db_track_user(uid, uname)
    is_auth = await db_is_owner_or_admin(uid)

    if is_auth:
        await send_msg(chat_id,
            "🌟 <b>ATLAS BOT — Admin Panel</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📄 <b>PDF Commands:</b>\n"
            "• <code>/pdf</code> — PDF reply করে MCQ generate + channel poll\n"
            "• <code>/pdfm</code> — PDF pagewise MCQ with image\n"
            "  Format: <code>/pdfm -p 1-5 -c @channel -m \"Topic\" 10</code>\n\n"
            "📸 <b>Image Commands:</b>\n"
            "• <code>/img</code> — Image reply করে MCQ poll channel-এ\n"
            "• <code>/pdfc</code> — একাধিক image → PDF বানাও\n"
            "• <code>/done</code> — Image collection শেষ করো\n\n"
            "📝 <b>Text/CSV Commands:</b>\n"
            "• <code>/txt</code> — Text reply করে MCQ poll\n"
            "• <code>/csv</code> — CSV reply করে channel poll\n"
            "• <code>/csvS</code> — CSV reply করে sequential poll\n\n"
            "📚 <b>Question Bank → PDF:</b>\n"
            "• <code>/qpdf</code> — chorcha.net mhtml/html reply করে Premium Q&A PDF\n\n"
            "🚀 <b>Rapid Fire (Scheduled, Comment-based):</b>\n"
            "• <code>/rapid [topic]</code> — CSV reply করে schedule করো\n"
            "  Channel + local time (যেমন 9:00 AM) select করার পর\n"
            "  নির্ধারিত সময়ে প্রতি 10s এ প্রশ্ন আসবে, 12s পর উত্তর reveal হবে\n\n"
            "🎯 <b>Live Quiz:</b>\n"
            "• <code>/live [topic]</code> — CSV reply করে Live Quiz শুরু\n"
            "• <code>/livetime [sec]</code> — প্রতি প্রশ্নের সময় set করো\n\n"
            "⚙️ <b>Settings:</b>\n"
            "• <code>/channel @id Name</code> — Channel/Group add করো\n"
            "• <code>/channelist</code> — Channel list দেখো\n"
            "• <code>/tagQ [text]</code> — Poll-এ tag set করো\n"
            "• <code>/expQ [text]</code> — Explanation footer set করো\n"
            "• <code>/permit [user_id]</code> — Admin add করো\n"
            "• <code>/remove [user_id]</code> — Admin remove করো\n"
            "• <code>/pinon</code> / <code>/pinoff</code> — Auto-pin on/off\n\n"
            "📊 <b>Info:</b>\n"
            "• <code>/info2</code> — Bot stats\n\n"
            "🔖 <b>Bookmark:</b>\n"
            "• <code>/bm</code> — Bookmark PDF বানাও\n"
            "• <code>/bmexam</code> — Bookmark MCQ থেকে Quiz\n\n"
            "🧩 <b>D1 Quiz System:</b>\n"
            "• <code>/q [name]</code> — CSV থেকে quiz তৈরি\n"
            "• <code>/qlist</code> — সব quiz দেখো\n"
            "• <code>/qdel [id]</code> — Quiz delete করো\n"
            "• <code>/pre [quiz_id]</code> — Quiz preview image set\n"
            "• <code>/info [quiz_id]</code> — Quiz details\n"
            "• <code>/send [quiz_id]</code> — Quiz share করো channel-এ\n"
            "• <code>/collect</code> — Poll collect mode on\n"
            "• <code>/merge</code> — Collected polls merge করো\n"
            "• <code>/convert [quiz_id]</code> — Quiz → CSV export\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🚀 <b>ATLAS BOT</b> — Atlascourses.com"
        )
    else:
        await send_msg(chat_id,
            f"🌟 <b>স্বাগতম {uname}..!</b>\n\n"
            "🚀 <b>ATLAS MCQ Bot</b> এ আপনাকে স্বাগতম!\n\n"
            "📚 <b>তোমার জন্য available commands:</b>\n\n"
            "🔖 <code>/bm</code> — Bookmark করা PDF বানাও (Practice Sheet)\n"
            "🎯 <code>/bmexam</code> — Bookmark MCQ থেকে Quiz দাও\n"
            "📸 <code>/pdfc</code> — একাধিক Image → একটা PDF বানাও\n"
            "✅ <code>/done</code> — Image collection শেষ করো\n"
            "❌ <code>/cancel</code> — চলমান কাজ বাতিল করো\n\n"
            "📌 কোনো Quiz link পেলে সরাসরি ক্লিক করলেই কুইজ শুরু হয়ে যাবে!\n\n"
            "❓ <code>/help</code> — আবার এই মেনু দেখতে চাইলে\n\n"
            "🚀 ATLAS — Atlascourses.com"
        )

# ============================================================
# FEATURE 2: UNAUTHORIZED
# ============================================================
UNAUTH_MSG = (
    "This Bot is Made By Amir Hamza Rafi.\n"
    "Please contact with Owner for using full power of this bot. [Paid]\n"
    "🚀 WhatsApp: wa.me/8801999681290"
)

# ============================================================
# FEATURE 3: /permit + /remove
# ============================================================
async def handle_permit(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text", "")
    if uid != OWNER_ID:
        await send_msg(chat_id, "❌ Owner only!")
        return
    args = text.split()
    if len(args) < 2:
        r = sb.table("admins").select("user_id").execute()
        admins = r.data or []
        txt = f"👑 Admins:\n• {OWNER_ID} (Owner)\n"
        for a in admins:
            txt += f"• {a['user_id']}\n"
        await send_msg(chat_id, txt)
        return
    target = int(args[1])
    sb.table("admins").upsert({"user_id": target}).execute()
    await send_msg(chat_id, f"✅ Admin added: {target}")

async def handle_remove(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text", "")
    if uid != OWNER_ID:
        await send_msg(chat_id, "❌ Owner only!")
        return
    args = text.split()
    if len(args) < 2:
        await send_msg(chat_id, "❌ /remove [user_id]")
        return
    target = int(args[1])
    sb.table("admins").delete().eq("user_id", target).execute()
    await send_msg(chat_id, f"✅ Admin removed: {target}")

# ============================================================
# FEATURE 4: /tagQ + /expQ
# ============================================================
async def handle_tagQ(msg: dict):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").replace("/tagQ", "").strip()
    if text:
        sb.table("quiz_settings").upsert({"id": 1, "tag": text}).execute()
        await send_msg(chat_id, f"✅ Tag set:\n{text}")
    else:
        s = await db_get_settings()
        await send_msg(chat_id, f"🔖 Current tag:\n{s.get('tag') or 'None'}\n\nSet: /tagQ [text]")

async def handle_expQ(msg: dict):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").replace("/expQ", "").strip()
    if text:
        sb.table("quiz_settings").upsert({"id": 1, "exp_footer": text}).execute()
        await send_msg(chat_id, f"✅ Footer set:\n{text}")
    else:
        s = await db_get_settings()
        await send_msg(chat_id, f"📝 Current footer:\n{s.get('exp_footer') or 'None'}\n\nSet: /expQ [text]")

# ============================================================
# FEATURE 5: /channel, /channelist
# ============================================================
async def handle_channel(msg: dict):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "")
    if text.strip() == "/channelist":
        return await _show_channel_list(chat_id)
    args = text.replace("/channel", "").strip()
    if not args or args == "list":
        return await _show_channel_list(chat_id)
    parts = args.split(maxsplit=1)
    channel_id = parts[0]
    custom_name = parts[1] if len(parts) > 1 else None
    if "t.me/" in channel_id:
        channel_id = "@" + channel_id.split("/")[-1]
    if channel_id.startswith("@") or channel_id.startswith("-100"):
        display = custom_name or channel_id
        sb.table("channels").upsert({
            "channel_id": channel_id,
            "channel_name": display
        }).execute()
        await send_msg(chat_id, f"✅ Channel added: {channel_id}\n📛 Name: {display}")
    else:
        await send_msg(chat_id,
            "❌ Invalid!\n\n"
            "<b>Usage:</b>\n"
            "<code>/channel @name</code>\n"
            "<code>/channel -100xxx Custom Name</code>\n"
            "<code>/channelist</code> — list all"
        )

async def _show_channel_list(chat_id):
    channels = await db_get_channels()
    if not channels:
        await send_msg(chat_id,
            "📢 No channels saved!\n\n"
            "Add: <code>/channel @name</code>\n"
            "Add: <code>/channel -100xxx Custom Name</code>"
        )
        return
    txt = "📢 <b>Saved Channels</b>\n\n"
    for i, ch in enumerate(channels, 1):
        ch_id = ch.get("channel_id", "")
        ch_name = ch.get("channel_name", ch_id)
        txt += f"{i}. 📢 <b>{ch_name}</b>\n   🔗 <code>{ch_id}</code>\n\n"
    txt += "<b>Commands:</b>\n"
    txt += "<code>/channel @id Name</code> — add/update\n"
    txt += "<code>/channelist</code> — view list"
    await send_msg(chat_id, txt)

# ============================================================
# FEATURE: /pin on | /pin off
# ============================================================
async def handle_pin(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text", "").strip()
    if not await db_is_owner_or_admin(uid):
        await send_msg(chat_id, "❌ Admin only!")
        return
    arg = text.replace("/pin", "").strip().lower()
    if arg == "on":
        await db_set_pin_setting(chat_id, True)
        PIN_ENABLED[chat_id] = True
        await send_msg(chat_id, "📌 Auto-pin চালু! Summary message আর /pdfm message pin হবে।")
    elif arg == "off":
        await db_set_pin_setting(chat_id, False)
        PIN_ENABLED[chat_id] = False
        await send_msg(chat_id, "📌 Auto-pin বন্ধ!")
    else:
        current = await db_get_pin_setting(chat_id)
        await send_msg(chat_id, f"📌 Pin status: {'✅ ON' if current else '❌ OFF'}\n\nChange: /pin on | /pin off")

async def try_pin_message(chat_id, message_id: int):
    """Channel-এ message pin করার চেষ্টা করে"""
    enabled = PIN_ENABLED.get(chat_id)
    if enabled is None:
        enabled = await db_get_pin_setting(chat_id)
        PIN_ENABLED[chat_id] = enabled
    if enabled:
        await tg_post("pinChatMessage", {
            "chat_id": chat_id,
            "message_id": message_id,
            "disable_notification": True
        })

# ============================================================
# FEATURE: /livetime (seconds)
# ============================================================
async def handle_livetime(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text", "").strip()
    if not await db_is_owner_or_admin(uid):
        await send_msg(chat_id, "❌ Admin only!")
        return
    arg = text.replace("/livetime", "").strip()
    if arg.isdigit():
        sec = int(arg)
        if sec < 5 or sec > 120:
            await send_msg(chat_id, "❌ 5 থেকে 120 সেকেন্ডের মধ্যে দাও!")
            return
        await db_set_live_time(chat_id, sec)
        await send_msg(chat_id, f"⚡ Live Quiz time set: {sec} সেকেন্ড প্রতি প্রশ্নে")
    else:
        current = await db_get_live_time(chat_id)
        await send_msg(chat_id, f"⚡ Current live quiz time: {current} সেকেন্ড\n\nChange: /livetime 15")

# ============================================================
# FEATURE: /poll — Poll Extract (see poll_extract.py)
# ============================================================
from poll_extract import handle_poll_extract


# ============================================================
# FEATURE: /img — Image reply → Poll
# ============================================================
async def handle_img_command(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text", "").strip()
    reply = msg.get("reply_to_message")

    # Topic extract from command: /img Physics Chapter 3
    topic = re.sub(r"^/img\s*", "", text, flags=re.IGNORECASE).strip() or "ATLAS Special MCQ"

    if not reply:
        await send_msg(chat_id, "❌ কোনো image-এ reply করে /img দাও!\n\nExample: image-এ reply করে <code>/img Physics</code>", parse_mode="HTML")
        return
    if not (reply.get("photo") or reply.get("document")):
        await send_msg(chat_id, "❌ Image-এ reply করতে হবে!")
        return

    if reply.get("photo"):
        file_id = reply["photo"][-1]["file_id"]
    else:
        file_id = reply["document"]["file_id"]

    session_key = f"img_cmd_{uid}"
    sb.table("quiz_sessions").upsert({
        "key": session_key,
        "data": json.dumps({"file_id": file_id, "msg_id": reply["message_id"], "topic": topic}),
        "updated_at": int(time.time())
    }).execute()

    kb = {"inline_keyboard": [
        [{"text": "🖼️ Image Mode (image সহ channel-এ যাবে)", "callback_data": f"imgmode_image_{uid}"}],
        [{"text": "📝 Topic Mode (শুধু MCQ Poll)", "callback_data": f"imgmode_topic_{uid}"}]
    ]}
    await send_msg(chat_id,
        f"📸 Image পাওয়া গেছে!\n📌 Topic: <b>{topic}</b>\n\nকোন mode-এ পাঠাবে?",
        reply_markup=kb, parse_mode="HTML"
    )

async def handle_img_mode(mode: str, uid: int, chat_id: int, user: dict):
    session_key = f"img_cmd_{uid}"
    row = sb.table("quiz_sessions").select("data").eq("key", session_key).execute()
    if not row.data:
        await send_msg(chat_id, "❌ Session expired!")
        return

    img_data = json.loads(row.data[0]["data"])
    file_id = img_data["file_id"]
    topic = img_data.get("topic", "ATLAS Special MCQ")

    channels = await db_get_channels()
    if not channels:
        await send_msg(chat_id, "❌ কোনো channel save করা নেই! /channel দিয়ে add করো।")
        return

    sb.table("quiz_sessions").upsert({
        "key": f"img_mode_{uid}",
        "data": json.dumps({"file_id": file_id, "mode": mode, "topic": topic}),
        "updated_at": int(time.time())
    }).execute()

    kb = {"inline_keyboard": []}
    for ch in channels:
        ch_id = ch.get("channel_id", "")
        ch_name = ch.get("channel_name", ch_id)
        kb["inline_keyboard"].append([{
            "text": f"📢 {ch_name}",
            "callback_data": f"imgchannel_{ch_id}_{uid}"
        }])
    await send_msg(chat_id, f"📢 কোন channel-এ পাঠাবে?\n📌 Topic: <b>{topic}</b>", reply_markup=kb, parse_mode="HTML")

async def process_img_to_poll(file_id: str, channel_id: str, mode: str,
                               chat_id: int, uid: int, uname: str, topic: str = "ATLAS Special MCQ"):
    settings = await db_get_settings()
    tag = settings.get("tag", "")
    exp_footer = settings.get("exp_footer", "")

    loading = await send_msg(chat_id, "⏳ Image থেকে MCQ তৈরি হচ্ছে... (~30s)")
    loading_id = loading.get("result", {}).get("message_id")

    try:
        img_bytes = await download_tg_file(file_id)
        from PIL import Image as PILImage
        img = PILImage.open(BytesIO(img_bytes))

        mcqs = await generate_mcq_from_image(img, topic, 1, None)
        if not mcqs:
            await send_msg(chat_id, "❌ MCQ generate হয়নি!")
            return

        image_msg_id = None

        if mode == "image":
            caption = ""
            if tag:
                caption = f"{tag}\n\n"
            caption += (
                f"⌛ATLAS Special MCQ System\n"
                f"🌟Topic: {topic}\n"
                f"📌Page No: 01\n"
                f"💎MCQ: {len(mcqs)}"
            )
            photo_r = await send_photo(channel_id, img_bytes, caption)
            if photo_r.get("ok"):
                image_msg_id = photo_r["result"]["message_id"]

        # ✅ CSV file generate করো — new format with varied answers
        try:
            import csv as _csv
            from io import StringIO as _SIO
            _out = _SIO()
            _wr = _csv.writer(_out, quoting=_csv.QUOTE_ALL)
            _wr.writerow(["questions","option1","option2","option3","option4","option5","answer","explanation","type","section"])
            for m in mcqs:
                opts = m.get("options", ["","","",""])
                while len(opts) < 5:
                    opts.append("")
                padded = (opts + ["","","","",""])[:5]
                ans_idx = {"A":0,"B":1,"C":2,"D":3,"E":4}.get(m.get("answer","A"), 0)
                ans_numeric = ans_idx + 1  # 1-based
                exp = m.get("explanation","")
                _wr.writerow([m.get("question",""), padded[0], padded[1], padded[2], padded[3], padded[4], ans_numeric, exp, 1, 1])
            csv_content = _out.getvalue().encode("utf-8-sig")
            csv_caption = (
                f"📄 CSV ফাইল — {topic}\n"
                f"💎 {len(mcqs)} MCQ\n\n"
                f"📌 Format: questions, option1-5, answer(numeric), explanation, type, section"
            )
            await send_document(
                chat_id, csv_content,
                f"ATLAS_{topic or 'MCQ'}.csv",
                caption=csv_caption, mime_type="text/csv"
            )
        except Exception as csv_err:
            logger.warning(f"[IMG] CSV send failed: {csv_err}")

        poll_links = []
        for i, mcq in enumerate(mcqs):
            opts = mcq.get("options", [])
            opts = [o[:100] for o in opts[:4]]  # max 100 chars per option
            ans_idx = {"A": 0, "B": 1, "C": 2, "D": 3}.get(mcq.get("answer", "A"), 0)
            q_text = mcq["question"][:295]
            if tag:
                q_text = f"{tag}\n\n{q_text}"
            q_text = q_text[:300]  # Telegram limit
            exp = mcq.get("explanation", "")
            if exp_footer:
                exp = f"{exp}\n{exp_footer}"
            poll_r = await send_poll(
                channel_id, q_text, opts, ans_idx,
                explanation=exp[:200],
                reply_to_message_id=image_msg_id
            )
            if poll_r.get("ok") and i == 0:
                msg_id = poll_r["result"]["message_id"]
                cid = str(channel_id)
                if cid.startswith("-100"):
                    poll_links.append(f"https://t.me/c/{cid[4:]}/{msg_id}")
                else:
                    poll_links.append(f"https://t.me/{cid.lstrip('@')}/{msg_id}")
            await asyncio.sleep(0.3)

        end_text = (
            f"🎯Topic: {topic}\n"
            f"🌟Page No: 01\n"
            f"🚀MCQ: {len(mcqs)}\n"
        )
        if poll_links:
            end_text += f"🔗First Poll Link:\n{poll_links[0]}"

        # ✅ নতুন: cache save করো যাতে buttons কাজ করে
        cache_id_img = gen_session_id()
        await db_save_mcq_cache(cache_id_img, cache_id_img, 1, topic, mcqs, poll_links,
                                file_id, image_msg_id, channel_id)

        exam_url = f"{HF_SPACE_URL}/exam/{cache_id_img}"
        quiz_url = f"https://t.me/atlasQuizProBot?start=pdf_{cache_id_img}"
        poll_url = f"https://t.me/atlasQuizProBot?start=poll_{cache_id_img}"

        end_kb = {"inline_keyboard": [
            [{"text": "📝 Quiz Solve", "url": quiz_url},
             {"text": "🔄 Poll Solve", "url": poll_url}],
            [{"text": "🌐 Web Exam", "url": exam_url},
             {"text": "💎 Premium PDF", "url": f"https://t.me/atlasQuizProBot?start=premium_{cache_id_img}"}]
        ]}

        end_r = await tg_post("sendMessage", {
            "chat_id": channel_id,
            "text": end_text,
            "reply_to_message_id": image_msg_id,
            "disable_web_page_preview": True,
            "reply_markup": end_kb
        })

        if end_r.get("ok"):
            end_msg_id = end_r["result"]["message_id"]
            await db_update_cache(cache_id_img, {"end_msg_id": end_msg_id})
            await try_pin_message(channel_id, end_msg_id)

        if loading_id:
            await edit_msg(chat_id, loading_id,
                f"✅ Done! {len(mcqs)} MCQ পাঠানো হয়েছে channel-এ।")

    except Exception as e:
        logger.error(f"[IMG] Error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")

# ============================================================
# FEATURE: /txt — Text reply → Poll
# ============================================================
async def handle_txt_command(msg: dict):
    """
    Text message-এ reply করে /txt দিলে MCQ CSV + channel list দেবে
    """
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    reply = msg.get("reply_to_message")

    if not reply or not reply.get("text"):
        await send_msg(chat_id, "❌ কোনো text message-এ reply করে /txt দাও!")
        return

    text_content = reply["text"]

    sb.table("quiz_sessions").upsert({
        "key": f"txt_cmd_{uid}",
        "data": json.dumps({"text": text_content[:5000]}),
        "updated_at": int(time.time())
    }).execute()

    channels = await db_get_channels()
    if not channels:
        await send_msg(chat_id, "❌ কোনো channel নেই! /channel দিয়ে add করো।")
        return

    kb = {"inline_keyboard": []}
    for ch in channels:
        ch_id = ch.get("channel_id", "")
        ch_name = ch.get("channel_name", ch_id)
        kb["inline_keyboard"].append([{
            "text": f"📢 {ch_name}",
            "callback_data": f"txtchannel_{ch_id}_{uid}"
        }])
    kb["inline_keyboard"].append([{
        "text": "📄 CSV File Only",
        "callback_data": f"txtchannel_csv_{uid}"
    }])
    await send_msg(chat_id,
        f"📝 Text পাওয়া গেছে! ({len(text_content)} chars)\nChannel select করো:",
        reply_markup=kb
    )

async def process_txt_to_poll(text_content: str, channel_id: str,
                               chat_id: int, uid: int, uname: str):
    """Text থেকে MCQ generate করে CSV + Poll পাঠাও"""
    import io, csv as csv_mod

    settings = await db_get_settings()
    tag = settings.get("tag", "")
    exp_footer = settings.get("exp_footer", "")

    loading = await send_msg(chat_id, "⏳ Text থেকে MCQ তৈরি হচ্ছে...")
    loading_id = loading.get("result", {}).get("message_id")

    try:
        from pdf_handler import generate_mcq_from_text
        mcqs = await generate_mcq_from_text(text_content, "ATLAS MCQ", count=15)

        if not mcqs:
            await send_msg(chat_id, "❌ MCQ generate হয়নি!")
            return

        buf = io.StringIO()
        writer = csv_mod.writer(buf)
        writer.writerow(["questions","option1","option2","option3","option4",
                          "answer","explanation","type","section"])
        for m in mcqs:
            opts = m.get("options", ["","","",""])
            ans_map = {"A":"1","B":"2","C":"3","D":"4"}
            ans_num = ans_map.get(m.get("answer","A"), "1")
            writer.writerow([m["question"], opts[0], opts[1],
                             opts[2] if len(opts)>2 else "",
                             opts[3] if len(opts)>3 else "",
                             ans_num, m.get("explanation",""), "1", "1"])
        await send_document(chat_id, buf.getvalue().encode("utf-8"),
            "ATLAS_mcq.csv", caption=f"📄 {len(mcqs)} MCQ CSV", mime_type="text/csv")

        if channel_id == "csv":
            if loading_id:
                await edit_msg(chat_id, loading_id, f"✅ CSV done! {len(mcqs)} MCQ")
            return

        for i, mcq in enumerate(mcqs):
            opts = mcq.get("options", [])
            ans_idx = {"A":0,"B":1,"C":2,"D":3}.get(mcq.get("answer","A"), 0)
            q_text = mcq["question"]
            if tag:
                q_text = f"{tag}\n\n{q_text}"
            exp = mcq.get("explanation","")
            if exp_footer:
                exp = f"{exp}\n{exp_footer}"
            await send_poll(channel_id, q_text, opts, ans_idx, explanation=exp[:200])
            await asyncio.sleep(0.3)

        if loading_id:
            await edit_msg(chat_id, loading_id,
                f"✅ {len(mcqs)} MCQ poll পাঠানো হয়েছে!")

    except Exception as e:
        logger.error(f"[TXT] Error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")

# ============================================================
# STEP 7 (ATLAS_CSV_GUIDE) — /csv + /csvS CORRECT IMPLEMENTATION
# ============================================================
# HELPER FUNCTIONS — CSV pre/end/summary messages
# ============================================================
def csv_get_pre_message(topic: str, count: int) -> str:
    topic_text = f'"{topic}"' if topic else ""
    return (
        f"🌟Important Poll Solve By ATLAS\n"
        f"🔥Topic Name: {topic_text}\n\n"
        f"✅প্রশ্ন সংখ্যা: {count}"
    )

def csv_get_ending_message(topic: str, count: int, first_link: str = "") -> str:
    topic_text = f'"{topic}"' if topic else ""
    base = (
        f"🎉 ধন্যবাদ প্রিয় শিক্ষার্থী!\n"
        f"👉এটলাস আয়োজিত {topic_text} পোল সলভে অংশগ্রহণ করার জন্য। 😊\n\n"
        f"📊 মোট পোল: {count}\n\n"
        f"⁉️তোমার স্কোর কত? 🤔\n"
        f"( ? / {count} )\n\n"
        f"নিচে লিখো! 👇"
    )
    if first_link:
        base += f"\n\n✅পোল যেখান থেকে শুরু হয়েছে:\n{first_link}"
    return base

def csv_get_master_summary(topic: str, total: int,
                            total_batches: int, batch_links: list) -> str:
    """
    batch_links = [(part_num, link, count), ...]
    """
    text = (
        f"🟥Poll Topic: \"{topic}\"\n"
        f"🌟মোট প্রশ্ন: {total}\n"
        f"📦 মোট ব্যাচ: {total_batches}\n\n"
    )
    for part_n, link, count in batch_links:
        text += f"📍Part-{part_n:02d}: ({count}টি প্রশ্ন)\n{link}\n\n"
    text += (
        "📌 *এটলাসের Exam Batch* এ অসংখ্য প্রশ্ন প্রাক্টিসের সুযোগ আছে।\n"
        "💬 *Whatsapp:* wa.me/8801999681290\n"
        "🌟 *Website:* Atlascourses.com"
    )
    return text

def _get_first_poll_link(channel_id: str, msg_id: int) -> str:
    """Poll message link বানাও"""
    cid = str(channel_id)
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{msg_id}"
    return f"https://t.me/{cid.lstrip('@')}/{msg_id}"

# ============================================================
# /csv COMMAND HANDLER
# Usage 1 (reply): CSV file reply করে /csv [topic]
# Usage 2 (inline): /csv (Topic Name) (channel/group id) (topic_id optional)
# ============================================================
async def handle_csv_command(msg: dict):
    """
    দুটো usage:
    1. CSV file-এ reply করে: /csv [topic]
       → Channel list দেখাবে
    2. Inline: /csv (Topic Name) (-100xxx or @ch) (topic_id)
       → CSV reply করে সরাসরি ওই channel/group topic-এ পাঠাবে
    """
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text", "").strip()
    reply = msg.get("reply_to_message")

    # Full text after /csv
    raw_args = text[len("/csv"):].strip()

    # Parse inline args: (Topic Name) (channel_id) (topic_id optional)
    # Format: /csv জাতীয় বাজেট -100123456789 12
    inline_channel = None
    inline_topic_id = None
    inline_topic_name = raw_args

    # Check if args contain a channel_id (-100... or @...) pattern
    import re as _re
    chan_match = _re.search(r'(-100\d+|@\S+)', raw_args)
    if chan_match:
        inline_channel = chan_match.group(1)
        before_chan = raw_args[:chan_match.start()].strip()
        after_chan = raw_args[chan_match.end():].strip()
        inline_topic_name = before_chan

        # topic_id is digits after channel_id
        tid_match = _re.match(r'(\d+)', after_chan)
        if tid_match:
            inline_topic_id = int(tid_match.group(1))

    topic = inline_topic_name or ""

    if not reply or not reply.get("document"):
        await send_msg(chat_id,
            "❌ CSV ফাইলে reply করে /csv দাও!\n\n"
            "<b>Usage 1 (reply mode):</b>\n"
            "<code>/csv জাতীয় বাজেট-২০২৬</code>\n\n"
            "<b>Usage 2 (inline mode):</b>\n"
            "<code>/csv Topic Name -100123456 [topic_id]</code>\n"
            "<code>/csv Topic Name @channel</code>\n\n"
            "📌 Topic optional — না দিলে blank থাকবে"
        )
        return

    doc = reply["document"]
    if not doc.get("file_name", "").lower().endswith(".csv"):
        await send_msg(chat_id, "❌ শুধু .csv file support করে!")
        return

    loading = await send_msg(chat_id, "⏳ CSV পড়া হচ্ছে...")
    loading_id = loading.get("result", {}).get("message_id")

    try:
        csv_bytes = await download_tg_file(doc["file_id"])
        mcqs = _parse_csv_bytes(csv_bytes)

        if not mcqs:
            await send_msg(chat_id, "❌ CSV-এ কোনো valid MCQ পাওয়া যায়নি!")
            return

        # Session save (topic + mcqs)
        cache_id = gen_session_id()
        await db_save_mcq_cache(cache_id, cache_id, 0, topic or "CSV MCQ", mcqs)

        sb.table("quiz_sessions").upsert({
            "key": f"csv_cmd_{uid}",
            "data": json.dumps({
                "cache_id": cache_id,
                "topic": topic,
                "mcq_count": len(mcqs),
                "mode": "csv",
                "inline_channel": inline_channel,
                "inline_topic_id": inline_topic_id
            }),
            "updated_at": int(time.time())
        }).execute()

        # Inline mode: directly send to specified channel
        if inline_channel:
            if loading_id:
                await edit_msg(chat_id, loading_id,
                    f"✅ {len(mcqs)} MCQ | 📢 সরাসরি {inline_channel}-এ পাঠানো হচ্ছে...")
            asyncio.create_task(process_csv_to_channel(
                cache_id, inline_channel, chat_id, uid
            ))
            return

        if loading_id:
            await edit_msg(chat_id, loading_id,
                f"✅ {len(mcqs)} MCQ পাওয়া গেছে!\n📢 কী করতে চাও?")

        # Action buttons — Quiz Solve, Poll Solve, Web Exam, Premium PDF
        kb = {"inline_keyboard": [
            [
                {"text": "🎯 Quiz Solve", "callback_data": f"csvact_quiz_{cache_id}_{uid}"},
                {"text": "📊 Poll Solve", "callback_data": f"csvact_poll_{cache_id}_{uid}"},
            ],
            [
                {"text": "🌐 Web Exam", "callback_data": f"csvact_web_{cache_id}_{uid}"},
                {"text": "📄 Premium PDF", "callback_data": f"csvact_pdf_{cache_id}_{uid}"},
            ],
            [{"text": "📢 Channel এ পাঠাও", "callback_data": f"csvact_channel_{cache_id}_{uid}"}],
            [{"text": "❌ Cancel", "callback_data": f"csvcancel_{uid}"}],
        ]}
        await send_msg(chat_id,
            f"✅ <b>{len(mcqs)} MCQ</b> | 🔥 {topic or 'N/A'}\n\nএকটা option select করো:",
            reply_markup=kb,
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"[CSV] Error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")

# ============================================================
# /csvS COMMAND HANDLER
# Usage: CSV file reply করে /csvS [batch_size] [topic]
# ============================================================
async def handle_csvs_command(msg: dict):
    """
    CSV file-এ reply করে /csvS [batch] [topic] দিলে:
    1. MCQs কে batch size-এ ভাগ করবে
    2. প্রতি batch-এ:
       - Part-01, Part-02... করে pre-message
       - সব polls
       - Ending message (ওই batch-এর first poll link সহ)
    3. সব শেষে Master Summary message
    """
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text", "").strip()
    reply = msg.get("reply_to_message")

    # Parse args: /csvS [batch_size] [topic]
    args = text.replace("/csvS", "").strip().split()
    if not args or not args[0].isdigit():
        await send_msg(chat_id,
            "❌ Correct format:\n"
            "<code>/csvS 25 জাতীয় বাজেট-২০২৬</code>\n\n"
            "📌 প্রথম number = batch size\n"
            "📌 বাকিটা = topic name"
        )
        return

    batch_size = int(args[0])
    topic = " ".join(args[1:]) if len(args) > 1 else "MCQ"

    if not reply or not reply.get("document"):
        await send_msg(chat_id, "❌ CSV ফাইলে reply করে /csvS দাও!")
        return

    loading = await send_msg(chat_id, "⏳ CSV পড়া হচ্ছে...")
    loading_id = loading.get("result", {}).get("message_id")

    try:
        csv_bytes = await download_tg_file(reply["document"]["file_id"])
        mcqs = _parse_csv_bytes(csv_bytes)

        if not mcqs:
            await send_msg(chat_id, "❌ CSV-এ MCQ নেই!")
            return

        # Session save
        cache_id = gen_session_id()
        await db_save_mcq_cache(cache_id, cache_id, 0, topic, mcqs)

        sb.table("quiz_sessions").upsert({
            "key": f"csv_cmd_{uid}",
            "data": json.dumps({
                "cache_id": cache_id,
                "topic": topic,
                "batch_size": batch_size,
                "mcq_count": len(mcqs),
                "mode": "csvs"  # serial/batch mode
            }),
            "updated_at": int(time.time())
        }).execute()

        batches = [mcqs[i:i+batch_size] for i in range(0, len(mcqs), batch_size)]

        if loading_id:
            await edit_msg(chat_id, loading_id,
                f"✅ {len(mcqs)} MCQ পাওয়া গেছে!\n"
                f"📦 {len(batches)} batch (প্রতিটায় {batch_size} টি)\n\n"
                f"📢 Channel select করো:")

        channels = await db_get_channels()
        if not channels:
            await send_msg(chat_id, "❌ Channel নেই!")
            return

        kb = {"inline_keyboard": []}
        for ch in channels:
            ch_id = ch.get("channel_id", "")
            ch_name = ch.get("channel_name", ch_id)
            kb["inline_keyboard"].append([{
                "text": f"📢 {ch_name}",
                "callback_data": f"csvchannel_{ch_id}_{uid}"
            }])
        kb["inline_keyboard"].append([{
            "text": "❌ Cancel",
            "callback_data": f"csvcancel_{uid}"
        }])
        await send_msg(chat_id,
            f"📊 {len(mcqs)} MCQ | Batch: {batch_size} | 🔥 {topic}\n\nChannel select করো:",
            reply_markup=kb
        )

    except Exception as e:
        logger.error(f"[CSVS] Error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")

# ============================================================
# SHARED CSV PARSER
# ============================================================
def _parse_csv_bytes(csv_bytes: bytes) -> list:
    """
    CSV bytes থেকে MCQ list বানাও।
    Reference: parse_csv_to_mcqs() from services.py
    """
    import io, csv as csv_mod_local
    try:
        content = csv_bytes.decode("utf-8-sig")
        reader = csv_mod_local.DictReader(io.StringIO(content))
        mcqs = []
        for row in reader:
            q = row.get("questions") or row.get("question", "")
            if not q:
                continue
            opts_raw = [
                row.get("option1", ""), row.get("option2", ""),
                row.get("option3", ""), row.get("option4", "")
            ]
            opts = [o.strip() for o in opts_raw if o.strip()]
            if len(opts) < 2:
                continue
            ans_raw = str(row.get("answer", "1")).strip().upper()
            ans_map = {
                "1": "A", "2": "B", "3": "C", "4": "D",
                "A": "A", "B": "B", "C": "C", "D": "D"
            }
            ans = ans_map.get(ans_raw, "A")
            mcqs.append({
                "question": q.strip(),
                "options": opts,
                "answer": ans,
                "explanation": row.get("explanation", "").strip()
            })
        return mcqs
    except Exception as e:
        logger.error(f"[CSV Parse] Error: {e}")
        return []

# ============================================================
# CORE POLL SENDER — CSV/CSVS উভয়ের জন্য
# ============================================================
async def _send_csv_polls_to_channel(
    channel_id: str, mcqs: list, topic: str,
    chat_id: int, pre_msg_id: int = None,
    thread_id: int = None
) -> tuple:
    """
    একটা batch-এর polls পাঠাও।
    Returns: (sent_count, first_poll_link)
    """
    settings = await db_get_settings()
    tag = settings.get("tag", "")
    exp_footer = settings.get("exp_footer", "")

    sent = 0
    first_poll_link = ""

    for i, mcq in enumerate(mcqs):
        opts = mcq.get("options", [])
        ans_idx = {"A": 0, "B": 1, "C": 2, "D": 3}.get(mcq.get("answer", "A"), 0)

        q_text = mcq["question"]
        if tag:
            q_text = f"{tag}\n\n{q_text}"

        exp = mcq.get("explanation", "")
        if exp_footer:
            exp = f"{exp}\n{exp_footer}"

        # Retry logic — poll অবশ্যই যেতে হবে
        for attempt in range(3):
            poll_r = await send_poll(
                channel_id, q_text, opts, ans_idx,
                explanation=exp[:200],
                reply_to_message_id=pre_msg_id,
                message_thread_id=thread_id
            )
            if poll_r.get("ok"):
                if sent == 0:
                    first_poll_link = _get_first_poll_link(
                        channel_id, poll_r["result"]["message_id"]
                    )
                sent += 1
                break
            else:
                logger.warning(f"[CSV] Poll {i+1} attempt {attempt+1} failed, retrying...")
                await asyncio.sleep(2)

        await asyncio.sleep(2.5)  # Rate limit (same as reference code)

    return sent, first_poll_link

async def process_csv_to_channel(cache_id: str, channel_id: str,
                                  chat_id: int, uid: int):
    """
    /csv — single batch, সব polls একসাথে পাঠাও
    /csvS — serial batch mode
    """
    row = sb.table("quiz_sessions").select("data").eq("key", f"csv_cmd_{uid}").execute()
    if not row.data:
        await send_msg(chat_id, "❌ Session expired!")
        return

    session = json.loads(row.data[0]["data"])
    topic = session.get("topic", "")
    mode = session.get("mode", "csv")
    thread_id = session.get("inline_topic_id") or None  # group topic/thread ID

    cache = await db_get_mcq_cache(cache_id)
    if not cache:
        await send_msg(chat_id, "❌ Cache expired!")
        return

    mcqs = cache["mcq_data"]
    total = len(mcqs)

    loading = await send_msg(chat_id, f"📤 {total} টি poll পাঠানো হচ্ছে...")
    loading_id = loading.get("result", {}).get("message_id")

    if mode == "csvs":
        # Serial/batch mode
        batch_size = session.get("batch_size", 25)
        batches = [mcqs[i:i+batch_size] for i in range(0, total, batch_size)]
        total_batches = len(batches)
        batch_links = []

        for b_idx, batch in enumerate(batches, 1):
            batch_topic = f"{topic} (Part-{b_idx:02d})"

            # Pre-message
            pre_text = csv_get_pre_message(batch_topic, len(batch))
            pre_r = await tg_post("sendMessage", {
                "chat_id": channel_id, "text": pre_text
            })
            pre_msg_id = pre_r.get("result", {}).get("message_id") if pre_r.get("ok") else None

            # Polls পাঠাও
            sent, first_link = await _send_csv_polls_to_channel(
                channel_id, batch, batch_topic, chat_id, pre_msg_id,
                thread_id=thread_id
            )

            # প্রতিটা batch-এর জন্য আলাদা cache — Quiz Solve/Poll Solve/Web Exam বাটনের জন্য
            batch_cache_id = gen_session_id()
            await db_save_mcq_cache(batch_cache_id, batch_cache_id, b_idx, batch_topic, batch)

            # Ending message for this batch
            ending = csv_get_ending_message(batch_topic, sent, first_link)
            exam_url = f"{HF_SPACE_URL}/exam/{batch_cache_id}"
            quiz_url = f"https://t.me/atlasQuizProBot?start=pdf_{batch_cache_id}"
            poll_url = f"https://t.me/atlasQuizProBot?start=poll_{batch_cache_id}"
            web_url  = f"https://atlasquizbotpro.hamza818483.workers.dev/quiz/{batch_cache_id}"
            end_kb = {"inline_keyboard": [
                [{"text": "📝 Quiz Solve", "url": quiz_url},
                 {"text": "🔄 Poll Solve", "url": poll_url}],
                [{"text": "🌐 Web Exam", "url": exam_url},
                 {"text": "📄 Premium PDF", "url": web_url}],
            ]}
            end_r = await tg_post("sendMessage", {
                "chat_id": channel_id,
                "text": ending,
                "disable_web_page_preview": True,
                "reply_markup": end_kb
            })
            if end_r.get("ok"):
                await db_update_cache(batch_cache_id, {
                    "channel_id": channel_id,
                    "end_msg_id": end_r["result"]["message_id"]
                })
            batch_links.append((b_idx, first_link, len(batch)))

            if loading_id:
                await edit_msg(chat_id, loading_id,
                    f"⏳ Batch {b_idx}/{total_batches} done — {sent} polls sent")

            await asyncio.sleep(2.5)

        # Master Summary (শুধু multiple batch হলে)
        if total_batches > 1:
            summary = csv_get_master_summary(topic, total, total_batches, batch_links)
            sum_r = await tg_post("sendMessage", {
                "chat_id": channel_id,
                "text": summary,
                "disable_web_page_preview": True
            })
            # Auto-pin summary
            if sum_r.get("ok"):
                await try_pin_message(channel_id, sum_r["result"]["message_id"])

        if loading_id:
            await edit_msg(chat_id, loading_id,
                f"✅ সব batch শেষ! {total} MCQ → {total_batches} batch")

    else:
        # Normal /csv mode — single batch
        pre_text = csv_get_pre_message(topic, total)
        pre_send_data = {"chat_id": channel_id, "text": pre_text}
        if thread_id:
            pre_send_data["message_thread_id"] = thread_id
        pre_r = await tg_post("sendMessage", pre_send_data)
        pre_msg_id = pre_r.get("result", {}).get("message_id") if pre_r.get("ok") else None

        sent, first_link = await _send_csv_polls_to_channel(
            channel_id, mcqs, topic, chat_id, pre_msg_id,
            thread_id=thread_id
        )

        ending = csv_get_ending_message(topic, sent, first_link)
        exam_url = f"{HF_SPACE_URL}/exam/{cache_id}"
        quiz_url = f"https://t.me/atlasQuizProBot?start=pdf_{cache_id}"
        poll_url = f"https://t.me/atlasQuizProBot?start=poll_{cache_id}"
        web_url  = f"https://atlasquizbotpro.hamza818483.workers.dev/quiz/{cache_id}"
        end_kb = {"inline_keyboard": [
            [{"text": "📝 Quiz Solve", "url": quiz_url},
             {"text": "🔄 Poll Solve", "url": poll_url}],
            [{"text": "🌐 Web Exam", "url": exam_url},
             {"text": "📄 Premium PDF", "url": web_url}],
        ]}
        end_send_data = {
            "chat_id": channel_id,
            "text": ending,
            "disable_web_page_preview": True,
            "reply_markup": end_kb
        }
        if thread_id:
            end_send_data["message_thread_id"] = thread_id
        end_r = await tg_post("sendMessage", end_send_data)
        if end_r.get("ok"):
            await db_update_cache(cache_id, {
                "channel_id": channel_id,
                "end_msg_id": end_r["result"]["message_id"]
            })

        if loading_id:
            await edit_msg(chat_id, loading_id,
                f"✅ {sent}/{total} polls channel-এ পাঠানো হয়েছে!")

async def handle_premium_pdf_start(msg: dict, cache_id: str):
    """Premium PDF button clicked — generate PDF from cache"""
    chat_id = msg["chat"]["id"]
    r = await send_msg(chat_id, "⏳ Premium PDF তৈরি হচ্ছে...")
    status_id = r.get("result", {}).get("message_id")
    try:
        cache = await db_get_mcq_cache(cache_id)
        if not cache:
            await send_msg(chat_id, "❌ Cache পাওয়া যায়নি!")
            return
        topic = cache.get("topic", "MCQ")
        mcqs = cache["mcq_data"]
        html = _build_rapid_pdf_html(topic, mcqs)
        pdf_bytes = await _html_to_pdf(html)
        if not pdf_bytes:
            await send_msg(chat_id, "❌ PDF generate হয়নি!")
            return
        safe = re.sub(r"[^\w\u0980-\u09FF]+", "_", topic)[:40] or "MCQ"
        if status_id:
            await tg_post("deleteMessage", {"chat_id": chat_id, "message_id": status_id})
        await send_document(chat_id, pdf_bytes,
            f"{safe}.pdf",
            caption=f"📄 <b>{topic}</b>\n💎 {len(mcqs)} MCQ",
            mime_type="application/pdf"
        )
    except Exception as e:
        await send_msg(chat_id, f"❌ PDF error: {e}")


async def handle_wm_command(msg: dict):
    """/wm (watermark text) — apply watermark to replied PDF or set default"""
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text", "").strip()
    wm_text = re.sub(r"^/wm\s*", "", text, flags=re.IGNORECASE).strip()
    reply = msg.get("reply_to_message")

    if not wm_text:
        await send_msg(chat_id,
            "📌 Usage:\n"
            "<code>/wm YourName</code> — reply করো যেকোনো PDF এ\n"
            "অথবা default watermark set করতে reply ছাড়াই দাও",
            parse_mode="HTML"
        )
        return

    # Default watermark save করো
    settings = await db_get_settings()
    settings["watermark"] = wm_text
    await db_save_settings(settings)

    # Reply PDF থাকলে সেটায় apply করো
    if reply and (reply.get("document") or reply.get("photo")):
        file_id = None
        if reply.get("document"):
            file_id = reply["document"]["file_id"]
        if file_id:
            await send_msg(chat_id, f"⏳ Watermark apply হচ্ছে: <b>{wm_text}</b>", parse_mode="HTML")
            asyncio.create_task(_apply_watermark_to_pdf(chat_id, file_id, wm_text))
            return

    await send_msg(chat_id,
        f"✅ Default watermark set: <b>{wm_text}</b>\n\n"
        f"এখন থেকে সব PDF এ এই watermark apply হবে।\n"
        f"যেকোনো পুরনো PDF এ reply করে <code>/wm {wm_text}</code> দিলে সেটায় apply হবে।",
        parse_mode="HTML"
    )


async def _apply_watermark_to_pdf(chat_id: int, file_id: str, wm_text: str):
    """Download PDF, apply watermark using existing add_watermark_to_pdf, resend"""
    try:
        pdf_bytes = await download_tg_file(file_id)
        wm_bytes = add_watermark_to_pdf(pdf_bytes, wm_text)
        await send_document(chat_id, wm_bytes,
            f"watermarked.pdf",
            caption=f"✅ Watermark applied: <b>{wm_text}</b>",
            mime_type="application/pdf"
        )
    except Exception as e:
        await send_msg(chat_id, f"❌ Watermark error: {e}")
async def handle_info2(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    if uid != OWNER_ID:
        await send_msg(chat_id, "❌ Owner only!")
        return
    try:
        users = sb.table("pdf_users").select("user_id", count="exact").execute()
        sessions = sb.table("pdf_sessions").select("id", count="exact").execute()
        web_exams = sb.table("web_exam_results").select("id", count="exact").execute()
        top_r = sb.table("web_exam_results").select("user_name, user_id").execute()
        top_counts = {}
        for row in (top_r.data or []):
            uid_r = row["user_id"]
            top_counts[uid_r] = {
                "name": row["user_name"],
                "count": top_counts.get(uid_r, {}).get("count", 0) + 1
            }
        top_sorted = sorted(top_counts.values(), key=lambda x: x["count"], reverse=True)[:3]
        medals = ["🥇", "🥈", "🥉"]
        txt = "📊 <b>ATLAS Bot Statistics</b>\n\n"
        txt += f"👥 Total Users: {users.count or 0}\n"
        txt += f"📄 PDF Sessions: {sessions.count or 0}\n"
        txt += f"🌐 Web Exams: {web_exams.count or 0}\n"
        txt += f"🔑 Gemini Keys: {len(key_rotator.keys)}\n\n"
        txt += "🔝 <b>Top Exam Takers:</b>\n"
        for i, u in enumerate(top_sorted):
            txt += f"{medals[i]} {u['name']} — {u['count']} exams\n"
        await send_msg(chat_id, txt)
    except Exception as e:
        await send_msg(chat_id, f"❌ Error: {e}")

# ============================================================
# FEATURE 7: /bm — Practice Sheet Style PDF
# ============================================================
async def handle_bm(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    try:
        r = sb.table("bookmarks").select("*").eq("user_id", uid).order("created_at").execute()
        bookmarks = r.data or []
        if not bookmarks:
            await send_msg(chat_id, "🔖 কোনো bookmark নেই!\n\nWeb Exam এ 🔖 বাটন চেপে bookmark করো।")
            return
        await send_msg(chat_id, f"🔖 {len(bookmarks)} টি bookmark পাওয়া গেছে!\n📄 PDF তৈরি হচ্ছে...")
        html = _build_bm_html(bookmarks)
        pdf_bytes = await _html_to_pdf(html)
        if pdf_bytes:
            await send_document(
                chat_id, pdf_bytes, "ATLAS_Bookmarks.pdf",
                caption=f"🔖 <b>ATLAS Bookmark Sheet</b>\n📝 {len(bookmarks)} MCQ",
                mime_type="application/pdf"
            )
        else:
            await send_msg(chat_id, "❌ PDF generate হয়নি!")
    except Exception as e:
        logger.error(f"[BM] Error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")

# ============================================================
# BM HTML — Practice Sheet exact style (2-col, boxed, Q+opts+ans+exp)
# ============================================================
def _build_bm_html(bookmarks: list) -> str:
    labels = ["A", "B", "C", "D"]
    items = ""
    for i, bm in enumerate(bookmarks, 1):
        q = bm.get("question_data", {})
        if isinstance(q, str):
            try:
                q = json.loads(q)
            except (json.JSONDecodeError, TypeError):
                q = {}
        if not isinstance(q, dict):
            q = {}
        opts = q.get("options", [])
        ans = q.get("answer", "")  # "A","B","C","D"
        ans_idx = {"A": 0, "B": 1, "C": 2, "D": 3}.get(ans, 0)
        topic = bm.get("topic", "")
        page = bm.get("page_number", "")

        opts_html = ""
        for j, opt in enumerate(opts):
            label = labels[j] if j < 4 else str(j + 1)
            cls = "opt correct" if label == ans else "opt"
            opts_html += f'<div class="{cls}">({label}) {opt}</div>'

        ans_label = labels[ans_idx] if ans_idx < 4 else ans
        ans_text = opts[ans_idx] if ans_idx < len(opts) else ""
        exp = q.get("explanation", "")

        items += f"""<div class="card">
  <div class="qno">{i:02d}.</div>
  <div class="qtxt">{q.get('question','')}</div>
  <div class="opts-wrap">{opts_html}</div>
  <div class="ans-row"><span class="ans-badge">['{ans_label}']</span></div>
  <div class="exp-box"><b>ব্যাখ্যা:</b> {exp}</div>
  <div class="meta">📌 {topic} | Page: {page}</div>
</div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;600;700;800&display=swap');
@page{{size:A4;margin:8mm 10mm;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Noto Sans Bengali',sans-serif;background:#fff;font-size:11px;}}
.hdr{{text-align:center;padding:10px 14px;background:#1a237e;color:#fff;margin-bottom:12px;border-radius:8px;}}
.hdr h1{{font-size:16px;font-weight:800;letter-spacing:.5px;}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;}}
.card{{background:#fff;border:1.5px solid #c5cae9;border-radius:8px;padding:9px 10px;break-inside:avoid;page-break-inside:avoid;}}
.qno{{font-size:10px;font-weight:800;color:#1a237e;margin-bottom:3px;}}
.qtxt{{font-size:12px;font-weight:700;color:#111;margin-bottom:7px;line-height:1.6;}}
.opts-wrap{{display:flex;flex-direction:column;gap:3px;margin-bottom:7px;}}
.opt{{font-size:11px;color:#333;padding:2px 6px;border-radius:4px;border:1px solid #e0e0e0;line-height:1.5;}}
.opt.correct{{background:#e8f5e9;border-color:#43a047;color:#1b5e20;font-weight:700;}}
.ans-row{{margin-bottom:4px;}}
.ans-badge{{font-size:10px;font-weight:800;color:#1b5e20;background:#f1f8e9;border:1px solid #81c784;border-radius:4px;padding:1px 7px;}}
.exp-box{{font-size:10.5px;color:#1a237e;background:#e8eaf6;border-left:3px solid #3949ab;padding:5px 7px;border-radius:0 5px 5px 0;line-height:1.55;margin-bottom:4px;}}
.meta{{font-size:9.5px;color:#9e9e9e;}}
.footer{{text-align:center;font-size:9px;color:#9e9e9e;margin-top:12px;}}
</style></head>
<body>
<div class="hdr"><h1>🔖 ATLAS Bookmark Sheet</h1></div>
<div class="grid">{items}</div>
<div class="footer">🚀 ATLAS Special MCQ System — Atlascourses.com</div>
</body></html>"""

# ============================================================
# FEATURE 7b: /bmexam — Bookmarks থেকে Poll Quiz
# ============================================================
async def handle_bmexam(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    try:
        r = sb.table("bookmarks").select("*").eq("user_id", uid).order("created_at").execute()
        bookmarks = r.data or []
        if not bookmarks:
            await send_msg(chat_id, "🔖 কোনো bookmark নেই!\n\nWeb Exam এ 🔖 বাটন চেপে bookmark করো।")
            return

        total = len(bookmarks)
        kb = {"inline_keyboard": [
            [{"text": f"✅ সব {total}টি Practice করো", "callback_data": f"bmex_all_{uid}"}],
        ]}
        if total > 10:
            kb["inline_keyboard"].insert(0,
                [{"text": "🔟 শেষ 10টি", "callback_data": f"bmex_10_{uid}"}])
        if total > 20:
            kb["inline_keyboard"].insert(0,
                [{"text": "2️⃣0️⃣ শেষ 20টি", "callback_data": f"bmex_20_{uid}"}])

        await send_msg(chat_id,
            f"🔖 <b>তোমার মোট {total}টি Bookmark MCQ আছে!</b>\n\n"
            f"কতগুলো নিয়ে practice করতে চাও?",
            reply_markup=kb
        )
    except Exception as e:
        logger.error(f"[BMEXAM] Error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")


async def handle_bmexam_start(chat_id: int, uid: int, uname: str, count_choice: str):
    """User count select করার পর — cache বানিয়ে Quiz Solve/Poll Solve/Web Exam বাটন দাও"""
    try:
        r = sb.table("bookmarks").select("*").eq("user_id", uid).order("created_at").execute()
        bookmarks = r.data or []
        if not bookmarks:
            await send_msg(chat_id, "🔖 কোনো bookmark নেই!")
            return

        if count_choice == "10":
            bookmarks = bookmarks[-10:]
        elif count_choice == "20":
            bookmarks = bookmarks[-20:]
        # "all" হলে সবগুলো

        mcqs = []
        for bm in bookmarks:
            q = bm.get("question_data", {})
            if isinstance(q, str):
                try:
                    q = json.loads(q)
                except (json.JSONDecodeError, TypeError):
                    q = {}
            if q and isinstance(q, dict):
                mcqs.append(q)

        if not mcqs:
            await send_msg(chat_id, "❌ Bookmark MCQ পাওয়া যায়নি!")
            return

        cache_id = gen_session_id()
        await db_save_mcq_cache(cache_id, cache_id, 0, "🔖 Bookmark Practice", mcqs)

        exam_url = f"{HF_SPACE_URL}/exam/{cache_id}"
        quiz_url = f"https://t.me/atlasQuizProBot?start=pdf_{cache_id}"
        poll_url = f"https://t.me/atlasQuizProBot?start=poll_{cache_id}"
        end_kb = {"inline_keyboard": [
            [{"text": "📝 Quiz Solve", "url": quiz_url}],
            [{"text": "🔄 Poll Solve", "url": poll_url}],
            [{"text": "🌐 Web Exam", "url": exam_url}]
        ]}
        await send_msg(chat_id,
            f"✅ <b>{len(mcqs)}টি Bookmark MCQ Ready!</b>\n\n"
            f"নিচের যেকোনো একটি বাটনে ক্লিক করে practice শুরু করো 👇",
            reply_markup=end_kb
        )
    except Exception as e:
        logger.error(f"[BMEXAM] start error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")

# ============================================================
# HTML → PDF (Chromium)
# ============================================================
async def _html_to_pdf(html: str) -> bytes:
    import tempfile
    chromium_bin = os.environ.get("CHROMIUM_PATH", "chromium")
    try:
        with tempfile.NamedTemporaryFile(suffix=".html", mode="w", encoding="utf-8", delete=False) as f:
            f.write(html)
            html_path = f.name
        pdf_path = html_path.replace(".html", ".pdf")
        proc = await asyncio.create_subprocess_exec(
            chromium_bin, "--headless", "--no-sandbox",
            "--disable-gpu", "--disable-dev-shm-usage",
            f"--print-to-pdf={pdf_path}",
            f"file://{html_path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
        if os.path.exists(pdf_path):
            with open(pdf_path, "rb") as f:
                return f.read()
        else:
            logger.error(f"[PDF Gen] chromium produced no file. stderr: {stderr.decode(errors='ignore')[:500]}")
    except FileNotFoundError:
        logger.error(f"[PDF Gen] chromium binary not found at '{chromium_bin}' — check Dockerfile install")
    except Exception as e:
        logger.error(f"[PDF Gen] Error: {e}")
    return None

# ============================================================
# FEATURE: /qpdf — chorcha.net mhtml/html (ক/খ ভান্ডার, CQ) → Premium PDF
# Usage: .mhtml বা .html ফাইলে reply করে /qpdf দাও
# ============================================================
async def handle_qpdf_command(msg: dict):
    chat_id = msg["chat"]["id"]
    reply = msg.get("reply_to_message")

    if not reply or not reply.get("document"):
        await send_msg(chat_id,
            "❌ chorcha.net থেকে save করা .mhtml/.html file-এ reply করে /qpdf দাও!\n\n"
            "<b>যেভাবে file বানাবে:</b>\n"
            "Chrome → পেজ খুলো (ক ভান্ডার/খ ভান্ডার/CQ) → Ctrl+S → "
            "Save as type: <code>Webpage, Single File (*.mhtml)</code>"
        )
        return

    doc = reply["document"]
    file_name = doc.get("file_name", "")
    if not (file_name.lower().endswith(".mhtml") or file_name.lower().endswith(".html") or file_name.lower().endswith(".htm")):
        await send_msg(chat_id, "❌ শুধু .mhtml বা .html file support করে!")
        return

    loading = await send_msg(chat_id, "⏳ File পড়া হচ্ছে...")
    loading_id = loading.get("result", {}).get("message_id")

    try:
        raw_bytes = await download_tg_file(doc["file_id"])
        data = await asyncio.to_thread(parse_chorcha_file, raw_bytes)

        if not data["items"]:
            if loading_id:
                await edit_msg(chat_id, loading_id, "❌ কোনো প্রশ্ন/উত্তর খুঁজে পাওয়া যায়নি! Format ভিন্ন হতে পারে।")
            return

        if loading_id:
            await edit_msg(chat_id, loading_id,
                f"✅ {len(data['items'])} টি প্রশ্ন পাওয়া গেছে!\n🎨 PDF বানানো হচ্ছে...")

        html_out = await build_chorcha_pdf_html(data)
        pdf_bytes = await _html_to_pdf(html_out)

        if not pdf_bytes:
            if loading_id:
                await edit_msg(chat_id, loading_id, "❌ PDF generate করতে সমস্যা হয়েছে!")
            return

        safe_title = re.sub(r"[^\w\u0980-\u09FF\-]+", "_", data["page_title"])[:50] or "ATLAS_QuestionBank"
        await send_document(chat_id, pdf_bytes, f"{safe_title}.pdf",
            caption=f"📚 {data['page_title']}\n📝 মোট প্রশ্ন: {len(data['items'])}\n🚀 ATLAS APP")

        if loading_id:
            await tg_post("deleteMessage", {"chat_id": chat_id, "message_id": loading_id})

    except Exception as e:
        logger.error(f"[QPDF] Error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")

# ============================================================
# SOLVE SHEET PDF — Practice Sheet same style (2-col, boxed)
# ============================================================
def _build_solve_sheet_html(topic: str, page: int, mcqs: list, answers: dict = None) -> str:
    answers = answers or {}
    labels = ["A", "B", "C", "D"]
    items = ""
    for i, q in enumerate(mcqs):
        ci = {"A": 0, "B": 1, "C": 2, "D": 3}.get(q.get("answer", "A"), 0)
        ua = answers.get(str(i))
        ans_label = labels[ci] if ci < 4 else str(ci + 1)
        ans_text = q.get("options", ["", "", "", ""])[ci] if ci < len(q.get("options", [])) else ""
        exp = q.get("explanation", "")

        opts_html = ""
        for j, opt in enumerate(q.get("options", [])):
            label = labels[j] if j < 4 else str(j + 1)
            cls = "opt"
            mark = ""
            if j == ci:
                cls += " correct"
                mark = " ✓"
            elif ua is not None and j == ua and ua != ci:
                cls += " wrong"
                mark = " ✗"
            opts_html += f'<div class="{cls}">({label}) {opt}{mark}</div>'

        items += f"""<div class="card">
  <div class="qno">{i+1:02d}.</div>
  <div class="qtxt">{q.get('question','')}</div>
  <div class="opts-wrap">{opts_html}</div>
  <div class="ans-row"><span class="ans-badge">['{ans_label}']</span></div>
  {f'<div class="exp-box"><b>ব্যাখ্যা:</b> {exp}</div>' if exp else ''}
</div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;600;700;800&display=swap');
@page{{size:A4;margin:8mm 10mm;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Noto Sans Bengali',sans-serif;background:#fff;font-size:11px;}}
.hdr{{text-align:center;padding:10px 14px;background:#1a237e;color:#fff;margin-bottom:12px;border-radius:8px;}}
.hdr h1{{font-size:16px;font-weight:800;}}
.hdr .sub{{font-size:11px;color:#c5cae9;margin-top:3px;}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;}}
.card{{background:#fff;border:1.5px solid #c5cae9;border-radius:8px;padding:9px 10px;break-inside:avoid;page-break-inside:avoid;}}
.qno{{font-size:10px;font-weight:800;color:#1a237e;margin-bottom:3px;}}
.qtxt{{font-size:12px;font-weight:700;color:#111;margin-bottom:7px;line-height:1.6;}}
.opts-wrap{{display:flex;flex-direction:column;gap:3px;margin-bottom:7px;}}
.opt{{font-size:11px;color:#333;padding:2px 6px;border-radius:4px;border:1px solid #e0e0e0;line-height:1.5;}}
.opt.correct{{background:#e8f5e9;border-color:#43a047;color:#1b5e20;font-weight:700;}}
.opt.wrong{{background:#ffebee;border-color:#e53935;color:#b71c1c;font-weight:600;}}
.ans-row{{margin-bottom:4px;}}
.ans-badge{{font-size:10px;font-weight:800;color:#1b5e20;background:#f1f8e9;border:1px solid #81c784;border-radius:4px;padding:1px 7px;}}
.exp-box{{font-size:10.5px;color:#1a237e;background:#e8eaf6;border-left:3px solid #3949ab;padding:5px 7px;border-radius:0 5px 5px 0;line-height:1.55;}}
.footer{{text-align:center;font-size:9px;color:#9e9e9e;margin-top:12px;}}
</style></head>
<body>
<div class="hdr">
  <h1>📋 ATLAS Solve Sheet</h1>
  <div class="sub">🎯 {topic} &nbsp;|&nbsp; 📄 Page No: {fmt_page(page)} &nbsp;|&nbsp; 📝 {len(mcqs)} MCQ</div>
</div>
<div class="grid">{items}</div>
<div class="footer">🚀 ATLAS Special MCQ System — Atlascourses.com</div>
</body></html>"""

# ============================================================
# FEATURE 8: /pdf COMMAND
# ============================================================
async def handle_pdf(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    uname = msg["from"].get("first_name", "User")
    text = msg.get("text", "")
    reply = msg.get("reply_to_message")
    if not reply or not reply.get("document"):
        await send_msg(chat_id,
            "❌ PDF ফাইলে reply করে <code>/pdf</code> দাও!\n\n"
            "<b>Example:</b>\n"
            "<code>/pdf -p 1-5 -c @channel -m \"Topic\" 10</code>\n"
            "<code>/pdf -p 2 -c -100xxx -t \"Group Topic\" 10</code>"
        )
        return
    params = parse_pdf_command(text)
    topic = params["topic"]
    if not topic:
        m_t = re.search(r'-t\s+"([^"]+)"', text) or re.search(r"-t\s+'([^']+)'", text) or re.search(r'-t\s+(\S+)', text)
        if m_t:
            topic = m_t.group(1)
    topic = topic or DEFAULT_TOPIC
    page_range = params["page_range"]
    channel_id = params["channel_id"]
    mcq_count = params["mcq_count"]
    thread_id = params.get("thread_id")
    file_name = reply["document"].get("file_name", "document.pdf")
    file_id = reply["document"]["file_id"]
    file_size = reply["document"].get("file_size", 0)

    status_r = await send_msg(chat_id, "⏳ PDF download হচ্ছে...")
    status_msg_id = status_r.get("result", {}).get("message_id")

    try:
        if status_msg_id:
            size_mb = round(file_size / 1024 / 1024, 1) if file_size else "?"
            await edit_msg(chat_id, status_msg_id,
                f"⏳ PDF download হচ্ছে...\n📄 File: {file_name}\n📦 Size: {size_mb} MB\n[░░░░░░░░░░ 0%]")

        pdf_bytes = await download_tg_file(file_id)

        if status_msg_id:
            await edit_msg(chat_id, status_msg_id,
                f"✅ Download complete!\n📄 File: {file_name}\n[██████████ 100%]\n⏳ PDF → Images converting...")

        pages = await asyncio.to_thread(pdf_to_images, pdf_bytes, page_range)
        if not pages:
            await send_msg(chat_id, "❌ Page পাওয়া যায়নি!")
            return

        if not channel_id:
            channels = await db_get_channels()
            if not channels:
                await process_pdf_pages(chat_id, uid, uname, pages, topic, mcq_count, None, True, file_name, status_msg_id, thread_id=thread_id)
                return
            app.state.pdf_cache = getattr(app.state, "pdf_cache", {})
            app.state.pdf_cache[f"pdf_img_{uid}"] = pages
            sb.table("quiz_sessions").upsert({
                "key": f"pdf_pending_{uid}",
                "data": json.dumps({"topic": topic, "mcq_count": mcq_count, "file_name": file_name, "status_msg_id": status_msg_id, "thread_id": thread_id, "file_id": file_id, "page_range": page_range}),
                "updated_at": int(time.time())
            }).execute()
            kb = {"inline_keyboard": []}
            for ch in channels:
                ch_id = ch.get("channel_id", "")
                ch_name = ch.get("channel_name", ch_id)
                kb["inline_keyboard"].append([{"text": f"📢 {ch_name}", "callback_data": f"pdfch_{ch_id}_{uid}"}])
            kb["inline_keyboard"].append([{"text": "📄 CSV File Only", "callback_data": f"pdfch_csv_{uid}"}])
            await send_msg(chat_id,
                f"📋 <b>{len(pages)} page পাওয়া গেছে</b>\n🎯 Topic: {topic}\n\nChannel select করো:",
                reply_markup=kb)
            return

        await process_pdf_pages(chat_id, uid, uname, pages, topic, mcq_count, channel_id, False, file_name, status_msg_id, thread_id=thread_id)
    except Exception as e:
        logger.error(f"[PDF] Handle error: {e}", exc_info=True)
        await send_msg(chat_id, f"❌ Error: {e}")
        await notify_owner(f"[PDF] Error for user {uid}:\n{e}")

# ============================================================
# FEATURE 9: PROCESS PDF PAGES
# ============================================================
def _build_dashboard(file_name, topic, pages, page_status, start_time, total_mcq, total_polls):
    elapsed = int(time.time() - start_time)
    mins, secs = divmod(elapsed, 60)
    done = sum(1 for s in page_status if s["done"])
    total = len(page_status)
    pct = int(done / total * 100) if total else 0
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    lines = [
        "⏳ <b>ATLAS PDF Processing...</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📄 File: {file_name}", f"🎯 Topic: {topic}", f"📋 Pages: {total} total",
        "━━━━━━━━━━━━━━━━━━━━━━"
    ]
    for s in page_status:
        if s["done"]:
            lines.append(f"✅ Page {fmt_page(s['page'])}: {s['mcq']} MCQ ✓")
        elif s["current"]:
            lines.append(f"⏳ Page {fmt_page(s['page'])}: Processing...")
        else:
            lines.append(f"⬜ Page {fmt_page(s['page'])}: Waiting")
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 Progress: {pct}% [{bar}]",
        f"⏱️ Elapsed: {mins}:{secs:02d}",
        f"📝 MCQ done: {total_mcq}",
        f"🔄 Polls sent: {total_polls}"
    ]
    return "\n".join(lines)

async def process_pdf_pages(
    chat_id: int, uid: int, uname: str,
    pages: list, topic: str, mcq_count: int,
    channel_id: str, csv_only: bool,
    file_name: str = "document.pdf",
    status_msg_id: int = None,
    thread_id: int = None
):
    settings = await db_get_settings()
    tag = settings.get("tag", "")
    exp_footer = settings.get("exp_footer", "")
    session_id = gen_session_id()
    await db_save_session(session_id, {
        "user_id": uid, "user_name": uname, "topic": topic,
        "channel_id": channel_id or "", "total_pages": len(pages),
        "processed_pages": 0, "status": "processing"
    })

    page_status = [{"page": p, "done": False, "current": False, "mcq": 0} for p, _ in pages]
    start_time = time.time()
    total_mcq = 0
    total_polls = 0

    if not status_msg_id:
        r = await send_msg(chat_id, "⏳ Processing শুরু হচ্ছে...")
        status_msg_id = r.get("result", {}).get("message_id")

    await edit_msg(chat_id, status_msg_id,
        _build_dashboard(file_name, topic, pages, page_status, start_time, total_mcq, total_polls))

    summary_pages = []
    all_mcqs_csv = []
    first_image_msg_id = None

    for idx, (page_num, img) in enumerate(pages):
        page_status[idx]["current"] = True
        await edit_msg(chat_id, status_msg_id,
            _build_dashboard(file_name, topic, pages, page_status, start_time, total_mcq, total_polls))

        try:
            mcqs = await generate_mcq_from_image(img, topic, page_num, mcq_count)
            if not mcqs:
                page_status[idx]["current"] = False
                page_status[idx]["done"] = True
                page_status[idx]["mcq"] = 0
                continue

            cache_id = gen_session_id()
            img_bytes = image_to_bytes(img)

            if csv_only:
                for m in mcqs:
                    opts = m.get("options", ["", "", "", ""])
                    opts = [re.sub(r'^[A-Da-dক-ঘ][)\.।]\s*', '', str(o)) for o in opts]
                    ans_map = {"A": "1", "B": "2", "C": "3", "D": "4"}
                    ans_num = ans_map.get(m.get("answer", "A"), "1")
                    all_mcqs_csv.append([m["question"], opts[0], opts[1], opts[2], opts[3], ans_num, m.get("explanation", ""), "1", "1"])
                await db_save_mcq_cache(cache_id, session_id, page_num, topic, mcqs)
            else:
                caption = ""
                if tag:
                    caption = f"{tag}\n\n"
                caption += f"🟥ATLAS Special MCQ System\n🎯Topic: {topic}\n🌟Page No: {fmt_page(page_num)}"

                photo_r = await send_photo(channel_id, img_bytes, caption, message_thread_id=thread_id)
                image_msg_id = None
                image_file_id = None
                if photo_r.get("ok"):
                    image_msg_id = photo_r["result"]["message_id"]
                    image_file_id = photo_r["result"]["photo"][-1]["file_id"]
                    if first_image_msg_id is None:
                        first_image_msg_id = image_msg_id

                poll_links = []
                first_poll_link = ""
                for i, mcq in enumerate(mcqs):
                    opts = mcq.get("options", [])
                    ans_idx = {"A": 0, "B": 1, "C": 2, "D": 3}.get(mcq.get("answer", "A"), 0)
                    q_text = mcq["question"]
                    if tag:
                        q_text = f"{tag}\n\n{q_text}"
                    exp = mcq.get("explanation", "")
                    if exp_footer:
                        exp = f"{exp}\n{exp_footer}"
                    # Retry logic — poll অবশ্যই যেতে হবে
                    poll_r = {"ok": False}
                    for _attempt in range(3):
                        poll_r = await send_poll(
                            channel_id, q_text, opts, ans_idx,
                            explanation=exp[:200],
                            reply_to_message_id=image_msg_id,
                            message_thread_id=thread_id
                        )
                        if poll_r.get("ok"):
                            break
                        logger.warning(f"[Poll] MCQ {i+1} attempt {_attempt+1} failed, retrying...")
                        await asyncio.sleep(2)
                    if poll_r.get("ok") and i == 0:
                        msg_id = poll_r["result"]["message_id"]
                        if str(channel_id).startswith("-100"):
                            first_poll_link = f"https://t.me/c/{str(channel_id)[4:]}/{msg_id}"
                        else:
                            first_poll_link = f"https://t.me/{str(channel_id).lstrip('@')}/{msg_id}"
                        poll_links.append(first_poll_link)
                    total_polls += 1
                    await asyncio.sleep(0.3)

                await db_save_mcq_cache(cache_id, session_id, page_num, topic, mcqs, poll_links, image_file_id, image_msg_id, channel_id)

                exam_url = f"{HF_SPACE_URL}/exam/{cache_id}"
                quiz_url = f"https://t.me/atlasQuizProBot?start=pdf_{cache_id}"
                poll_url = f"https://t.me/atlasQuizProBot?start=poll_{cache_id}"

                end_data = {
                    "chat_id": channel_id,
                    "text": f"🚀🎯Topic: {topic}\n🌟Page No: {fmt_page(page_num)}\n🔗First Poll: {first_poll_link}",
                    "reply_markup": {"inline_keyboard": [
                        [{"text": "📝 Quiz Solve", "url": quiz_url}],
                        [{"text": "🔄 Poll Again", "url": poll_url}],
                        [{"text": "🌐 Website Exam", "url": exam_url}]
                    ]},
                    "reply_to_message_id": image_msg_id
                }
                if thread_id:
                    end_data["message_thread_id"] = thread_id
                end_r = await tg_post("sendMessage", end_data)
                if end_r.get("ok"):
                    await db_update_cache(cache_id, {"end_msg_id": end_r["result"]["message_id"]})

                summary_pages.append({"page": page_num, "first_poll": first_poll_link, "mcq_count": len(mcqs)})

                for m in mcqs:
                    opts = m.get("options", ["", "", "", ""])
                    opts = [re.sub(r'^[A-Da-dক-ঘ][)\.।]\s*', '', str(o)) for o in opts]
                    ans_map = {"A": "1", "B": "2", "C": "3", "D": "4"}
                    ans_num = ans_map.get(m.get("answer", "A"), "1")
                    all_mcqs_csv.append([m["question"], opts[0], opts[1], opts[2], opts[3], ans_num, m.get("explanation", ""), "1", "1"])

            total_mcq += len(mcqs)
            page_status[idx]["done"] = True
            page_status[idx]["current"] = False
            page_status[idx]["mcq"] = len(mcqs)
            await edit_msg(chat_id, status_msg_id,
                _build_dashboard(file_name, topic, pages, page_status, start_time, total_mcq, total_polls))
            sb.table("pdf_sessions").update({"processed_pages": page_num}).eq("id", session_id).execute()

        except Exception as e:
            logger.error(f"[PDF] Page {page_num} error: {e}", exc_info=True)
            page_status[idx]["current"] = False
            page_status[idx]["done"] = True
            await notify_owner(f"[PDF] Page {page_num} error:\n{e}")

    if all_mcqs_csv:
        import io, csv as csv_mod
        buf = io.StringIO()
        writer = csv_mod.writer(buf)
        writer.writerow(["questions","option1","option2","option3","option4","answer","explanation","type","section"])
        for row in all_mcqs_csv:
            writer.writerow(row)
        await send_document(chat_id, buf.getvalue().encode("utf-8"), f"{topic}_mcq.csv",
            caption=f"📄 {topic} — {len(all_mcqs_csv)} MCQ", mime_type="text/csv")

    if not csv_only and summary_pages:
        total_mcq_sum = sum(p["mcq_count"] for p in summary_pages)
        summary = f"🟥ATLAS Special Practice System\n🎯Topic: {topic}\n🚀Total MCQ: {total_mcq_sum}\n\n"
        for p in summary_pages:
            summary += f"🌟Page-{fmt_page(p['page'])}:\n{p['first_poll']}\n"
        summary += (
            f"\n💥শুভকামনা প্রিয় শিক্ষার্থী {uname}...\n"
            '"যেকোনো প্রশ্ন থাকলে মেসেজ দাও "Ask Your Mentor" গ্রুপে।\n'
            "🚀Whatsapp Helpline: wa.me/8801999681290\n🔗Website: Atlascourses.com"
        )
        summary_data = {"chat_id": channel_id, "text": summary, "disable_web_page_preview": True}
        if first_image_msg_id:
            summary_data["reply_to_message_id"] = first_image_msg_id
        await tg_post("sendMessage", summary_data)

    sb.table("pdf_sessions").update({"status": "done"}).eq("id", session_id).execute()
    elapsed = int(time.time() - start_time)
    mins, secs = divmod(elapsed, 60)
    await edit_msg(chat_id, status_msg_id,
        f"✅ <b>Processing Complete!</b>\n\n📄 File: {file_name}\n🎯 Topic: {topic}\n📝 Total MCQ: {total_mcq}\n📋 Pages: {len(pages)}\n⏱️ Time: {mins}:{secs:02d}")

# ============================================================
# FEATURE: /pdfm — PDF pagewise MCQ to channel
# Usage: /pdfm -p 1-5 -c @channel -m "Topic" -t topicId 10
# ============================================================
async def handle_pdfm(msg: dict):
    """
    /pdfm -p (pages) -c (channel) -m (topic) -t (thread_id) [mcq_count]

    -p না থাকলে: all pages
    -c না থাকলে: channel list → select → poll
    -m না থাকলে: "ATLAS MCQ"
    """
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    uname = msg["from"].get("first_name","User")
    text = msg.get("text","")
    reply = msg.get("reply_to_message")

    if not reply or not reply.get("document"):
        await send_msg(chat_id,
            "❌ PDF ফাইলে reply করে /pdfm দাও!\n\n"
            "<b>Format:</b>\n"
            "<code>/pdfm -p 1-5 -c @channel -m \"Topic\" -t group_id [5]</code>\n\n"
            "📌 -p = page range (না দিলে সব page)\n"
            "📌 -c = channel id (না দিলে list দেখাবে)\n"
            "📌 -m = topic name\n"
            "📌 -t = topic/thread id (group হলে)\n"
            "📌 [N] = per page MCQ count (bracket সহ)"
        )
        return

    params = _parse_pdfm_params(text)
    topic = params["topic"] or "🌟ATLAS MCQ"
    page_range = params["page_range"]
    channel_id = params["channel_id"]
    mcq_count = params["mcq_count"]
    thread_id = params["thread_id"]

    file_id = reply["document"]["file_id"]
    file_name = reply["document"].get("file_name","document.pdf")
    file_size = reply["document"].get("file_size",0)

    status_r = await send_msg(chat_id, "⏳ PDF download হচ্ছে...")
    status_msg_id = status_r.get("result",{}).get("message_id")

    try:
        pdf_bytes = await download_tg_file(file_id)
        pages = await asyncio.to_thread(pdf_to_images, pdf_bytes, page_range)

        if not pages:
            await send_msg(chat_id, "❌ Page পাওয়া যায়নি!")
            return

        if status_msg_id:
            await edit_msg(chat_id, status_msg_id,
                f"✅ {len(pages)} page পাওয়া গেছে!\n⏳ Processing...")

        if not channel_id:
            channels = await db_get_channels()
            if not channels:
                await process_pdfm_pages(chat_id, uid, uname, pages, topic,
                    mcq_count, None, True, file_name, status_msg_id, thread_id)
                return

            app.state.pdf_cache = getattr(app.state, "pdf_cache", {})
            app.state.pdf_cache[f"pdfm_img_{uid}"] = pages
            sb.table("quiz_sessions").upsert({
                "key": f"pdfm_pending_{uid}",
                "data": json.dumps({
                    "topic": topic, "mcq_count": mcq_count,
                    "file_name": file_name, "status_msg_id": status_msg_id,
                    "thread_id": thread_id,
                    "file_id": file_id,
                    "page_range": page_range
                }),
                "updated_at": int(time.time())
            }).execute()

            kb = {"inline_keyboard": []}
            for ch in channels:
                ch_id = ch.get("channel_id","")
                ch_name = ch.get("channel_name", ch_id)
                kb["inline_keyboard"].append([{
                    "text": f"📢 {ch_name}",
                    "callback_data": f"pdfmch_{ch_id}_{uid}"
                }])
            kb["inline_keyboard"].append([{
                "text": "📄 CSV Only",
                "callback_data": f"pdfmch_csv_{uid}"
            }])
            await send_msg(chat_id,
                f"📋 <b>{len(pages)} page</b>\n🎯 Topic: {topic}\n\nChannel select করো:",
                reply_markup=kb
            )
            return

        await process_pdfm_pages(chat_id, uid, uname, pages, topic,
            mcq_count, channel_id, False, file_name, status_msg_id, thread_id)

    except Exception as e:
        logger.error(f"[PDFM] Error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")

def _parse_pdfm_params(text: str) -> dict:
    """
    /pdfm -p 1-5 -c @channel -m "Topic" -t 123 10
    সব parameter parse করো
    """
    result = {
        "page_range": None,
        "channel_id": None,
        "topic": None,
        "thread_id": None,
        "mcq_count": None
    }

    m = re.search(r'-p\s+([\d,\-]+)', text)
    if m:
        result["page_range"] = parse_page_range(m.group(1))

    m = re.search(r'-c\s+(@\S+|-100\d+)', text)
    if m:
        result["channel_id"] = m.group(1)

    m = re.search(r'-m\s+"([^"]+)"', text) or re.search(r"-m\s+'([^']+)'", text) or re.search(r'-m\s+(\S+)', text)
    if m:
        result["topic"] = m.group(1)

    m = re.search(r'-t\s+(\d+)', text)
    if m:
        result["thread_id"] = int(m.group(1))

    m_bracket = re.search(r'\[(\d+)\]', text)
    if m_bracket:
        result["mcq_count"] = int(m_bracket.group(1))
    else:
        cleaned = re.sub(r'-[pcmt]\s+\S+', '', text)
        m2 = re.search(r'(\d+)\s*$', cleaned)
        if m2:
            result["mcq_count"] = int(m2.group(1))

    return result

async def process_pdfm_pages(
    chat_id: int, uid: int, uname: str,
    pages: list, topic: str, mcq_count,
    channel_id, csv_only: bool,
    file_name: str = "document.pdf",
    status_msg_id: int = None,
    thread_id: int = None
):
    """
    /pdfm এর main processing — /pdf এর মতো কিন্তু নতুন caption format সহ।
    Caption format:
      ⌛ATLAS Special MCQ System
      🌟Topic: (Topic Name)
      📌Page No: (count)
      💎MCQ: (count)

    End message format:
      🎯Topic: ...
      🌟Page No: ...
      🚀MCQ: (count)
      🔗First Poll Link: (link)

    Summary message format:
      ⚙️Summary সহ page count ও MCQ count
    """
    settings = await db_get_settings()
    tag = settings.get("tag","")
    exp_footer = settings.get("exp_footer","")
    session_id = gen_session_id()

    page_status = [{"page":p,"done":False,"current":False,"mcq":0} for p,_ in pages]
    start_time = time.time()
    total_mcq = 0
    total_polls = 0

    if not status_msg_id:
        r = await send_msg(chat_id, "⏳ Processing শুরু হচ্ছে...")
        status_msg_id = r.get("result",{}).get("message_id")

    await edit_msg(chat_id, status_msg_id,
        _build_dashboard(file_name, topic, pages, page_status, start_time, 0, 0))

    summary_pages = []
    all_mcqs_csv = []
    first_image_msg_id = None

    for idx, (page_num, img) in enumerate(pages):
        page_status[idx]["current"] = True
        await edit_msg(chat_id, status_msg_id,
            _build_dashboard(file_name, topic, pages, page_status, start_time, total_mcq, total_polls))

        try:
            mcqs = await generate_mcq_from_image(img, topic, page_num, mcq_count)
            if not mcqs:
                page_status[idx]["current"] = False
                page_status[idx]["done"] = True
                continue

            cache_id = gen_session_id()
            img_bytes = image_to_bytes(img)

            if csv_only:
                for m in mcqs:
                    opts = m.get("options",["","","",""])
                    ans_map = {"A":"1","B":"2","C":"3","D":"4"}
                    ans_num = ans_map.get(m.get("answer","A"),"1")
                    all_mcqs_csv.append([m["question"],opts[0],
                        opts[1] if len(opts)>1 else "",
                        opts[2] if len(opts)>2 else "",
                        opts[3] if len(opts)>3 else "",
                        ans_num, m.get("explanation",""),"1","1"])
                await db_save_mcq_cache(cache_id, session_id, page_num, topic, mcqs)
            else:
                caption = ""
                if tag:
                    caption = f"{tag}\n\n"
                caption += (
                    f"⌛ATLAS Special MCQ System\n"
                    f"🌟Topic: {topic}\n"
                    f"📌Page No: {fmt_page(page_num)}\n"
                    f"💎MCQ: {len(mcqs)}"
                )

                photo_r = await send_photo(channel_id, img_bytes, caption,
                    message_thread_id=thread_id)
                image_msg_id = None
                image_file_id = None
                if photo_r.get("ok"):
                    image_msg_id = photo_r["result"]["message_id"]
                    image_file_id = photo_r["result"]["photo"][-1]["file_id"]
                    if first_image_msg_id is None:
                        first_image_msg_id = image_msg_id

                poll_links = []
                first_poll_link = ""
                for i, mcq in enumerate(mcqs):
                    opts = mcq.get("options",[])
                    ans_idx = {"A":0,"B":1,"C":2,"D":3}.get(mcq.get("answer","A"),0)
                    q_text = mcq["question"]
                    if tag:
                        q_text = f"{tag}\n\n{q_text}"
                    exp = mcq.get("explanation","")
                    if exp_footer:
                        exp = f"{exp}\n{exp_footer}"
                    # Retry logic — poll অবশ্যই যেতে হবে
                    poll_r = {"ok": False}
                    for _attempt in range(3):
                        poll_r = await send_poll(
                            channel_id, q_text, opts, ans_idx,
                            explanation=exp[:200],
                            reply_to_message_id=image_msg_id,
                            message_thread_id=thread_id
                        )
                        if poll_r.get("ok"):
                            break
                        logger.warning(f"[Poll] MCQ {i+1} attempt {_attempt+1} failed, retrying...")
                        await asyncio.sleep(2)
                    if poll_r.get("ok") and i == 0:
                        pmid = poll_r["result"]["message_id"]
                        cid = str(channel_id)
                        first_poll_link = (
                            f"https://t.me/c/{cid[4:]}/{pmid}"
                            if cid.startswith("-100")
                            else f"https://t.me/{cid.lstrip('@')}/{pmid}"
                        )
                        poll_links.append(first_poll_link)
                    total_polls += 1
                    await asyncio.sleep(0.3)

                await db_save_mcq_cache(cache_id, session_id, page_num, topic, mcqs,
                    poll_links, image_file_id, image_msg_id, channel_id)

                end_text = (
                    f"🎯Topic: {topic}\n"
                    f"🌟Page No: {fmt_page(page_num)}\n"
                    f"🚀MCQ: {len(mcqs)}\n"
                    f"🔗First Poll Link:\n{first_poll_link}"
                )
                end_data = {
                    "chat_id": channel_id,
                    "text": end_text,
                    "reply_to_message_id": image_msg_id,
                    "disable_web_page_preview": True
                }
                if thread_id:
                    end_data["message_thread_id"] = thread_id
                end_r = await tg_post("sendMessage", end_data)

                if end_r.get("ok"):
                    end_msg_id = end_r["result"]["message_id"]
                    await db_update_cache(cache_id, {"end_msg_id": end_msg_id})

                summary_pages.append({
                    "page": page_num,
                    "first_poll": first_poll_link,
                    "mcq_count": len(mcqs)
                })

                for m in mcqs:
                    opts = m.get("options",["","","",""])
                    ans_map = {"A":"1","B":"2","C":"3","D":"4"}
                    ans_num = ans_map.get(m.get("answer","A"),"1")
                    all_mcqs_csv.append([m["question"],opts[0],
                        opts[1] if len(opts)>1 else "",
                        opts[2] if len(opts)>2 else "",
                        opts[3] if len(opts)>3 else "",
                        ans_num, m.get("explanation",""),"1","1"])

            total_mcq += len(mcqs)
            page_status[idx]["done"] = True
            page_status[idx]["current"] = False
            page_status[idx]["mcq"] = len(mcqs)
            await edit_msg(chat_id, status_msg_id,
                _build_dashboard(file_name, topic, pages, page_status, start_time, total_mcq, total_polls))

        except Exception as e:
            logger.error(f"[PDFM] Page {page_num} error: {e}")
            page_status[idx]["current"] = False
            page_status[idx]["done"] = True

    # CSV send
    if all_mcqs_csv:
        import io, csv as csv_mod
        buf = io.StringIO()
        writer = csv_mod.writer(buf)
        writer.writerow(["questions","option1","option2","option3","option4",
                          "answer","explanation","type","section"])
        for row in all_mcqs_csv:
            writer.writerow(row)
        await send_document(chat_id, buf.getvalue().encode("utf-8"),
            f"{topic}_mcq.csv",
            caption=f"📄 {topic} — {len(all_mcqs_csv)} MCQ",
            mime_type="text/csv")

    # Summary message
    if not csv_only and summary_pages:
        total_mcq_sum = sum(p["mcq_count"] for p in summary_pages)
        bd_time = _get_bd_time()
        summary = f"⚙️Summary\n🎯Topic: {topic}\n🚀Total MCQ: {total_mcq_sum}\n\n"
        for p in summary_pages:
            summary += f"🌟Page No: {fmt_page(p['page'])} ({p['mcq_count']} MCQ)\n{p['first_poll']}\n\n"
        summary += f"📅 {bd_time}"

        summary_data = {
            "chat_id": channel_id,
            "text": summary,
            "disable_web_page_preview": True
        }
        if first_image_msg_id:
            summary_data["reply_to_message_id"] = first_image_msg_id
        if thread_id:
            summary_data["message_thread_id"] = thread_id

        sum_r = await tg_post("sendMessage", summary_data)
        if sum_r.get("ok"):
            await try_pin_message(channel_id, sum_r["result"]["message_id"])

    elapsed = int(time.time() - start_time)
    mins, secs = divmod(elapsed, 60)
    await edit_msg(chat_id, status_msg_id,
        f"✅ <b>PDFM Complete!</b>\n\n📄 {file_name}\n🎯 {topic}\n"
        f"📝 Total MCQ: {total_mcq}\n📋 Pages: {len(pages)}\n⏱️ {mins}:{secs:02d}")

def _get_bd_time() -> str:
    """Bangladesh current time"""
    try:
        bd_tz = pytz.timezone("Asia/Dhaka")
        now = datetime.now(bd_tz)
        return now.strftime("%d %B %Y, %I:%M %p")
    except:
        return ""

# ============================================================
# FEATURE: /rapid — CSV রিপ্লাই করে Topic দিলে, channel + local time select
# করার পর, সেই সময়ে topic message পাঠিয়ে প্রতি 10s এ একটা করে প্রশ্ন
# (Comment-এ ছাত্ররা উত্তর দেবে), প্রশ্ন আসার 12s পর reply করে উত্তর+ব্যাখ্যা।
# শেষে topic message কে reply করে closing message, আর শুধু Q+A+Explanation
# এর একটা PDF (CSV-এর option ছাড়া) admin-কে পাঠানো হয়।
# ============================================================
RAPID_Q_INTERVAL = 10   # সেকেন্ড — প্রতি প্রশ্নের গ্যাপ
RAPID_ANS_DELAY = 8     # সেকেন্ড — প্রশ্ন আসার পর উত্তর reveal (8s পর answer, তারপর 2s এ নতুন প্রশ্ন)

_RAPID_ANS_EMOJIS = ["✅", "🎯", "💡", "🔥", "📌", "⭐"]


def _rapid_get_answer_text(mcq: dict) -> str:
    """mcq['answer'] হলো letter (A-D) — options থেকে আসল answer text বের করো।"""
    opts = mcq.get("options", [])
    idx = {"A": 0, "B": 1, "C": 2, "D": 3}.get(mcq.get("answer", "A"), 0)
    return opts[idx] if idx < len(opts) else (opts[0] if opts else "")


async def handle_rapid_command(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text", "").strip()
    reply = msg.get("reply_to_message")

    topic = text.replace("/rapid", "").strip()
    if not topic:
        await send_msg(chat_id,
            "❌ Correct format:\n<code>/rapid টপিক নাম</code>\n\n"
            "📌 CSV ফাইলে reply করে দাও।"
        )
        return

    if not reply or not reply.get("document"):
        await send_msg(chat_id, "❌ CSV ফাইলে reply করে /rapid (Topic Name) দাও!")
        return

    doc = reply["document"]
    if not doc.get("file_name", "").lower().endswith(".csv"):
        await send_msg(chat_id, "❌ শুধু .csv file support করে!")
        return

    loading = await send_msg(chat_id, "⏳ CSV পড়া হচ্ছে...")
    loading_id = loading.get("result", {}).get("message_id")

    try:
        csv_bytes = await download_tg_file(doc["file_id"])
        mcqs = _parse_csv_bytes(csv_bytes)
        if not mcqs:
            if loading_id:
                await edit_msg(chat_id, loading_id, "❌ CSV-এ কোনো valid প্রশ্ন পাওয়া যায়নি!")
            return

        RAPID_PENDING[uid] = {
            "step": "awaiting_channel",
            "topic": topic,
            "mcqs": mcqs,
            "admin_chat": chat_id,
        }

        if loading_id:
            await edit_msg(chat_id, loading_id,
                f"✅ {len(mcqs)} টি প্রশ্ন পাওয়া গেছে!\n📢 Channel select করো:")

        channels = await db_get_channels()
        if not channels:
            await send_msg(chat_id, "❌ Channel নেই! /channel দিয়ে add করো।")
            RAPID_PENDING.pop(uid, None)
            return

        kb = {"inline_keyboard": []}
        for ch in channels:
            ch_id = ch.get("channel_id", "")
            ch_name = ch.get("channel_name", ch_id)
            kb["inline_keyboard"].append([{
                "text": f"📢 {ch_name}",
                "callback_data": f"rapidch_{ch_id}_{uid}"
            }])
        kb["inline_keyboard"].append([{
            "text": "❌ Cancel",
            "callback_data": f"rapidcancel_{uid}"
        }])
        await send_msg(chat_id,
            f"🚀 Topic: {topic}\n📝 প্রশ্ন: {len(mcqs)} টি\n\n📢 Channel select করো:",
            reply_markup=kb
        )

    except Exception as e:
        logger.error(f"[RAPID] error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")


def _parse_local_time_text(text: str):
    """'9:00 AM', '10:02 PM', '21:15' ইত্যাদি parse করে (hour24, minute) রিটার্ন করে। Fail হলে None।"""
    text = text.strip().upper().replace(".", "")
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)?$", text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    ampm = m.group(3)
    if minute > 59:
        return None
    if ampm:
        if hour < 1 or hour > 12:
            return None
        if ampm == "AM":
            hour24 = 0 if hour == 12 else hour
        else:
            hour24 = 12 if hour == 12 else hour + 12
    else:
        if hour > 23:
            return None
        hour24 = hour
    return hour24, minute


async def handle_rapid_time_text(msg: dict) -> bool:
    """uid যদি RAPID_PENDING-এ awaiting_time state-এ থাকে, এই text কে time হিসেবে নেয়।
    Consumed হলে True রিটার্ন করে (handle_message এর router-কে জানানোর জন্য)।"""
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]
    state = RAPID_PENDING.get(uid)
    if not state or state.get("step") != "awaiting_time":
        return False

    text = (msg.get("text") or "").strip()
    parsed = _parse_local_time_text(text)
    if not parsed:
        await send_msg(chat_id,
            "❌ সময়ের ফরম্যাট ঠিক নেই!\n\n"
            "<b>Example:</b> <code>9:00 AM</code> অথবা <code>10:02 PM</code>"
        )
        return True

    hour24, minute = parsed
    bd_tz = pytz.timezone("Asia/Dhaka")
    now = datetime.now(bd_tz)
    run_at = now.replace(hour=hour24, minute=minute, second=0, microsecond=0)
    if run_at <= now:
        run_at += timedelta(days=1)

    state["step"] = "scheduled"
    job_id = gen_session_id()
    state["job_id"] = job_id
    state["run_at_ts"] = run_at.timestamp()

    # persist (so a restart before fire-time doesn't silently lose it — see _recover_rapid_jobs)
    sb.table("quiz_sessions").upsert({
        "key": f"rapid_job_{job_id}",
        "data": json.dumps({
            "topic": state["topic"],
            "mcqs": state["mcqs"],
            "channel_id": state["channel_id"],
            "admin_chat": state["admin_chat"],
            "run_at_ts": state["run_at_ts"],
            "status": "pending",
        }),
        "updated_at": int(time.time())
    }).execute()

    delay = (run_at - now).total_seconds()
    task = asyncio.create_task(_rapid_wait_and_run(job_id, delay))
    RAPID_TASKS[job_id] = task

    await send_msg(chat_id,
        f"✅ <b>Scheduled!</b>\n\n"
        f"🚀 Topic: {state['topic']}\n"
        f"📝 প্রশ্ন: {len(state['mcqs'])} টি\n"
        f"📢 Channel: <code>{state['channel_id']}</code>\n"
        f"🕐 সময়: {run_at.strftime('%d %B, %I:%M %p')} (BD time)\n\n"
        f"⏳ নির্ধারিত সময়ে শুরু হবে।"
    )
    RAPID_PENDING.pop(uid, None)
    return True


async def _rapid_wait_and_run(job_id: str, delay_seconds: float):
    try:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        await _run_rapid_job(job_id)
    except asyncio.CancelledError:
        pass
    finally:
        RAPID_TASKS.pop(job_id, None)


async def _run_rapid_job(job_id: str):
    row = sb.table("quiz_sessions").select("data").eq("key", f"rapid_job_{job_id}").execute()
    if not row.data:
        return
    job = json.loads(row.data[0]["data"])
    if job.get("status") == "done":
        return  # already ran (defensive, in case of duplicate triggers)

    topic = job["topic"]
    mcqs = job["mcqs"]
    channel_id = job["channel_id"]
    admin_chat = job["admin_chat"]
    total = len(mcqs)

    try:
        topic_text = (
            f"🌟 ATLAS Rapid Fire 🌟\n\n"
            f"🚀 Topic: {topic}\n"
            f"📝 প্রশ্ন সংখ্যা: {total}\n\n"
            f"✍️ Comment-এ উত্তর লিখো! প্রতি {RAPID_Q_INTERVAL} সেকেন্ডে নতুন প্রশ্ন আসবে।"
        )
        topic_r = await tg_post("sendMessage", {"chat_id": channel_id, "text": topic_text})
        topic_msg_id = topic_r.get("result", {}).get("message_id") if topic_r.get("ok") else None

        async def _reveal_answer(i, mcq, q_msg_id):
            """প্রশ্ন আসার RAPID_ANS_DELAY সেকেন্ড পর উত্তর reply করে — প্রশ্নের
            নিজের 10s cadence থেকে independent, যাতে timeline ovelap করতে পারে
            (spec অনুযায়ী: Q প্রতি 10s, কিন্তু A প্রতিটা Q এর 12s পরে)।"""
            await asyncio.sleep(RAPID_ANS_DELAY)
            ans_text = _rapid_get_answer_text(mcq)
            emoji = _RAPID_ANS_EMOJIS[i % len(_RAPID_ANS_EMOJIS)]
            reveal = f"{emoji} <b>সঠিক উত্তর:</b> {ans_text}"
            exp = mcq.get("explanation", "").strip()
            if exp:
                reveal += f"\n\n📖 <b>ব্যাখ্যা:</b> {exp}"
            await tg_post("sendMessage", {
                "chat_id": channel_id,
                "text": reveal,
                "parse_mode": "HTML",
                "reply_to_message_id": q_msg_id or topic_msg_id
            })

        reveal_tasks = []
        for i, mcq in enumerate(mcqs, 1):
            q_text = f"❓ প্রশ্ন {i}/{total}\n\n{mcq['question']}"
            q_r = await tg_post("sendMessage", {
                "chat_id": channel_id,
                "text": q_text,
                "reply_to_message_id": topic_msg_id
            })
            q_msg_id = q_r.get("result", {}).get("message_id") if q_r.get("ok") else None

            reveal_tasks.append(asyncio.create_task(_reveal_answer(i, mcq, q_msg_id)))

            if i < total:
                await asyncio.sleep(RAPID_Q_INTERVAL)

        # সব প্রশ্ন পাঠানো শেষ — কিন্তু শেষ ১-২টা প্রশ্নের উত্তর reveal হতে তখনও
        # কিছু সময় বাকি থাকতে পারে (RAPID_ANS_DELAY > RAPID_Q_INTERVAL হলে)।
        # Closing message-টা সব উত্তর reveal হওয়ার পরেই পাঠাও।
        if reveal_tasks:
            await asyncio.gather(*reveal_tasks)

        closing = (
            f"🎉 ধন্যবাদ! \"{topic}\" এর {total} টি প্রশ্ন শেষ হলো।\n\n"
            f"⁉️ কতগুলো সঠিক করতে পেরেছো? কমেন্টে জানাও! 👇"
        )
        await tg_post("sendMessage", {
            "chat_id": channel_id,
            "text": closing,
            "reply_to_message_id": topic_msg_id
        })

        # PDF → channel-এ topic_msg reply হিসেবে + admin-এও কপি
        try:
            html_out = _build_rapid_pdf_html(topic, mcqs)
            pdf_bytes = await _html_to_pdf(html_out)
            if pdf_bytes:
                safe_topic = re.sub(r"[^\w\u0980-\u09FF\-]+", "_", topic)[:40] or "Rapid"
                pdf_fname = f"{safe_topic}_Rapid_QA.pdf"
                pdf_caption = (
                    f"📄 <b>{topic}</b> — Rapid Fire Q+A\n"
                    f"📝 {total} টি প্রশ্ন | উত্তর + ব্যাখ্যা সহ"
                )
                # Channel-এ first message (topic_msg) reply হিসেবে
                await send_document(channel_id, pdf_bytes, pdf_fname,
                    caption=pdf_caption, mime_type="application/pdf",
                    reply_to_message_id=topic_msg_id)
                # Admin-এও কপি
                await send_document(admin_chat, pdf_bytes, pdf_fname,
                    caption=f"✅ \"{topic}\" Rapid Fire শেষ!\n📝 {total} টি প্রশ্ন\n📄 Q+A+Explanation PDF")
        except Exception as e:
            logger.error(f"[RAPID] PDF error: {e}")

        sb.table("quiz_sessions").update({"data": json.dumps({**job, "status": "done"})}) \
            .eq("key", f"rapid_job_{job_id}").execute()

    except Exception as e:
        logger.error(f"[RAPID] job {job_id} run error: {e}")
        try:
            await send_msg(admin_chat, f"❌ /rapid \"{topic}\" চালাতে সমস্যা হয়েছে: {e}")
        except Exception:
            pass


def _build_rapid_pdf_html(topic: str, mcqs: list) -> str:
    """শুধু Question + Answer + Explanation — CSV এর option ছাড়া।"""
    items = ""
    for i, mcq in enumerate(mcqs, 1):
        ans_text = _rapid_get_answer_text(mcq)
        exp = mcq.get("explanation", "").strip()
        items += f"""<div class="qa-box">
  <div class="q-row"><span class="q-no">{i}.</span>
    <div class="q-text">{html_lib.escape(mcq['question'])}</div></div>
  <div class="a-row"><span class="a-label">উত্তর:</span>
    <div class="a-text">{html_lib.escape(ans_text)}</div></div>
  {f'<div class="exp-row"><span class="exp-label">ব্যাখ্যা:</span><div class="exp-text">{html_lib.escape(exp)}</div></div>' if exp else ''}
</div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;600;700;800&display=swap');
@page{{size:A4;margin:12mm 10mm;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Noto Sans Bengali',sans-serif;background:#fff;color:#1a1a2e;font-size:12.5px;}}
.hdr{{text-align:center;padding:16px 18px;background:linear-gradient(135deg,#1a237e,#3949ab);color:#fff;border-radius:10px;margin-bottom:14px;}}
.hdr h1{{font-size:18px;font-weight:800;}}
.hdr .sub{{font-size:12px;color:#c5cae9;margin-top:5px;}}
.qa-box{{border:1.5px solid #d4dce6;border-radius:9px;margin-bottom:9px;overflow:hidden;break-inside:avoid;page-break-inside:avoid;}}
.q-row{{display:flex;gap:8px;background:#eef4fb;padding:9px 12px;border-bottom:1px solid #d4dce6;}}
.q-no{{font-weight:800;color:#0d4a8f;flex-shrink:0;}}
.q-text{{color:#0d2438;font-weight:600;line-height:1.6;}}
.a-row{{display:flex;gap:8px;background:#fff8ec;padding:8px 12px;}}
.a-label{{font-weight:800;color:#a15c00;flex-shrink:0;}}
.a-text{{color:#4a3000;line-height:1.6;}}
.exp-row{{display:flex;gap:8px;background:#eefaf1;padding:8px 12px;border-top:1px solid #d4dce6;}}
.exp-label{{font-weight:800;color:#1b5e20;flex-shrink:0;}}
.exp-text{{color:#1b3a1f;line-height:1.6;}}
.footer{{text-align:center;font-size:9.5px;color:#9aa5b1;margin-top:14px;}}
</style></head>
<body>
<div class="hdr"><h1>🚀 {html_lib.escape(topic)}</h1><div class="sub">Rapid Fire — Q + A + Explanation</div></div>
{items}
<div class="footer">🚀 ATLAS APP — Atlascourses.com</div>
</body></html>"""


async def _recover_rapid_jobs():
    """App restart হলে যেসব /rapid job এখনো fire হয়নি (run_at_ts ভবিষ্যতে), সেগুলো
    আবার schedule করে। Past হয়ে গেলে (process অনেকক্ষণ বন্ধ ছিল) মিস হিসেবে গণ্য করে skip করে।"""
    try:
        rows = sb.table("quiz_sessions").select("key,data") \
            .like("key", "rapid_job_%").execute()
        now_ts = time.time()
        for row in (rows.data or []):
            try:
                job = json.loads(row["data"])
            except Exception:
                continue
            if job.get("status") == "done":
                continue
            run_at_ts = job.get("run_at_ts", 0)
            job_id = row["key"].replace("rapid_job_", "")
            if run_at_ts <= now_ts:
                logger.warning(f"[RAPID] job {job_id} missed its scheduled time during downtime — skipping")
                sb.table("quiz_sessions").update({"data": json.dumps({**job, "status": "missed"})}) \
                    .eq("key", row["key"]).execute()
                continue
            delay = run_at_ts - now_ts
            task = asyncio.create_task(_rapid_wait_and_run(job_id, delay))
            RAPID_TASKS[job_id] = task
            logger.info(f"[RAPID] recovered job {job_id}, fires in {int(delay)}s")
    except Exception as e:
        logger.error(f"[RAPID] recovery error: {e}")


# ============================================================
# FEATURE: /live — Live Quiz System (v4)
# ============================================================
LIVE_POLL_MAP = {}  # poll_id -> group_id

async def handle_live_command(msg: dict):
    chat_id = msg["chat"]["id"]
    uid     = msg["from"]["id"]
    text    = msg.get("text", "").strip()
    reply   = msg.get("reply_to_message")

    topic = text.replace("/live", "").strip()
    if not topic:
        topic = "ATLAS Live Quiz"

    if not reply or not reply.get("document"):
        await send_msg(chat_id,
            "❌ CSV ফাইলে reply করে /live দাও!\n\n"
            "<b>Example:</b>\n"
            "<code>/live জাতীয় বাজেট-২০২৬</code>"
        )
        return

    doc = reply.get("document", {})
    if not doc.get("file_name", "").lower().endswith(".csv"):
        await send_msg(chat_id, "❌ শুধু .csv file support করে!")
        return

    loading    = await send_msg(chat_id, "⏳ CSV পড়া হচ্ছে...")
    loading_id = loading.get("result", {}).get("message_id")

    try:
        csv_bytes  = await download_tg_file(doc["file_id"])
        mcqs       = _parse_csv_bytes(csv_bytes)

        if not mcqs:
            await send_msg(chat_id, "❌ CSV-এ কোনো MCQ পাওয়া যায়নি!")
            return

        session_id = gen_session_id()

        sb.table("quiz_sessions").upsert({
            "key":        f"live_pending_{uid}",
            "data":       json.dumps({
                "session_id": session_id,
                "topic":      topic,
                "mcqs":       mcqs,
                "admin_chat": chat_id
            }),
            "updated_at": int(time.time())
        }).execute()

        if loading_id:
            await edit_msg(chat_id, loading_id,
                f"✅ {len(mcqs)} MCQ পাওয়া গেছে!\n"
                f"📢 Group select করো:")

        channels = await db_get_channels()
        if not channels:
            await send_msg(chat_id, "❌ কোনো group save নেই! /channel দিয়ে add করো।")
            return

        kb = {"inline_keyboard": []}
        for ch in channels:
            ch_id   = ch.get("channel_id", "")
            ch_name = ch.get("channel_name", ch_id)
            kb["inline_keyboard"].append([{
                "text":          f"📢 {ch_name}",
                "callback_data": f"livechannel_{ch_id}_{uid}"
            }])
        kb["inline_keyboard"].append([{
            "text":          "❌ Cancel",
            "callback_data": f"livecancel_{uid}"
        }])

        await send_msg(chat_id,
            f"🎯 Topic: {topic}\n"
            f"📝 MCQ: {len(mcqs)} টি\n\n"
            f"📢 Group select করো:",
            reply_markup=kb
        )

    except Exception as e:
        logger.error(f"[LIVE] error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")


async def start_live_quiz(group_id, session_id: str, topic: str,
                           mcqs: list, admin_chat: int, per_q_time: int, *args, **kwargs):
    """Live Quiz main runner (v4). Backward-compatible signature."""
    bd_time = _get_bd_time()
    total   = len(mcqs)

    pre_text = (
        f"🌟ATLAS Live Quiz🌟\n\n"
        f"🚀Topic: {topic}\n"
        f"🔗সময়: {bd_time}\n"
        f"🎯MCQ: {total} টি\n"
        f"⚡Per Quiz Time: {per_q_time} sec\n\n"
        f"Are Your Ready?"
    )
    await tg_post("sendMessage", {"chat_id": group_id, "text": pre_text})

    await asyncio.sleep(2)
    await tg_post("sendMessage", {"chat_id": group_id, "text": "3️⃣"})
    await asyncio.sleep(2)
    await tg_post("sendMessage", {"chat_id": group_id, "text": "2️⃣"})
    await asyncio.sleep(2)
    await tg_post("sendMessage", {"chat_id": group_id, "text": "1️⃣ 🚀 শুরু!"})
    await asyncio.sleep(1)

    quiz_start = time.time()
    live_state = {
        "session_id":          session_id,
        "topic":               topic,
        "mcqs":                mcqs,
        "total":               total,
        "current_idx":         0,
        "per_q_time":          per_q_time,
        "group_id":            group_id,
        "admin_chat":          admin_chat,
        "active":              True,
        "quiz_start_time":     quiz_start,
        "scores":              {},
        "current_poll_id":     None,
        "current_poll_msg_id": None,
        "current_correct_idx": 0,
        # নতুন: কে কোন option দাগিয়েছে track করার জন্য
        "option_voters":       {},  # poll_id -> {option_idx: [user_name, ...]}
    }
    LIVE_QUIZ_STATE[group_id] = live_state

    settings   = await db_get_settings()
    tag        = settings.get("tag", "")
    exp_footer = settings.get("exp_footer", "")

    for idx, mcq in enumerate(mcqs):
        if not LIVE_QUIZ_STATE.get(group_id, {}).get("active"):
            break

        live_state["current_idx"]         = idx
        live_state["current_correct_idx"] = {"A": 0, "B": 1, "C": 2, "D": 3}.get(
            mcq.get("answer", "A"), 0
        )

        opts    = mcq.get("options", [])
        ans_idx = live_state["current_correct_idx"]

        q_text = f"[{idx+1}/{total}] {mcq['question']}"
        if tag:
            q_text = f"{tag}\n\n{q_text}"

        exp = mcq.get("explanation", "")
        if exp_footer:
            exp = f"{exp}\n{exp_footer}"

        # Try non-anonymous first (for groups with View Votes feature)
        poll_r = await tg_post("sendPoll", {
            "chat_id":           group_id,
            "question":          q_text[:300],
            "options":           opts[:4],
            "type":              "regular",
            "is_anonymous":      False,
            "open_period":       per_q_time
        })

        # Fallback: If channel doesn't allow non-anonymous, use anonymous
        if not poll_r.get("ok") and "non-anonymous" in str(poll_r.get("description", "")):
            logger.warning(f"[LIVE] Target {group_id} is a channel, switching to anonymous poll")
            await send_msg(admin_chat, 
                f"⚠️ <b>Live Quiz Warning</b>\n\nTarget <code>{group_id}</code> is a <b>channel</b>.\nLive Quiz works best in <b>groups</b> for View Votes feature.\n\n✅ Quiz will continue with anonymous voting.")
            poll_r = await tg_post("sendPoll", {
                "chat_id":           group_id,
                "question":          q_text[:300],
                "options":           opts[:4],
                "type":              "regular",
                "is_anonymous":      True,
                "open_period":       per_q_time
            })

        poll_id = ""
        poll_msg_id = None
        if poll_r.get("ok"):
            poll_id = poll_r["result"].get("poll", {}).get("id", "")
            poll_msg_id = poll_r["result"]["message_id"]
            live_state["current_poll_id"] = poll_id
            live_state["current_poll_msg_id"] = poll_msg_id
            if poll_id:
                LIVE_POLL_MAP[poll_id] = group_id


        else:
            logger.warning(
                f"[LIVE] Poll {idx+1} failed: {poll_r.get('description')}"
            )

        # ✅ Timer শেষ হলে instant next — 0.5s delay only (আগে per_q_time + 2 ছিল)
        # open_period দিয়ে Telegram নিজেই timer manage করে।
        # আমরা শুধু open_period সেকেন্ড wait করব, তারপর 0.5s buffer।
        await asyncio.sleep(per_q_time + 0.5)

        if poll_r.get("ok"):
            try:
                # Stop the poll to show results
                stop_result = await tg_post("stopPoll", {
                    "chat_id":    group_id,
                    "message_id": poll_r["result"]["message_id"]
                })

                # ✅ Send correct answer reveal message
                correct_idx = live_state.get("current_correct_idx", 0)
                correct_letter = ["A", "B", "C", "D"][correct_idx] if correct_idx < 4 else "A"
                correct_option = opts[correct_idx] if correct_idx < len(opts) else opts[0]

                reveal_text = (
                    f"✅ <b>Correct Answer:</b> ({correct_letter}) {correct_option}\n"
                    f"📖 <b>Explanation:</b> {exp[:200]}"
                )
                await tg_post("sendMessage", {
                    "chat_id": group_id,
                    "text": reveal_text,
                    "parse_mode": "HTML",
                    "reply_to_message_id": poll_r["result"]["message_id"],
                    "disable_notification": True
                })
            except Exception:
                pass
            if poll_id:
                LIVE_POLL_MAP.pop(poll_id, None)

        # ✅ Instant next — 0s delay
        await asyncio.sleep(0)

    live_state["active"] = False
    LIVE_QUIZ_STATE.pop(group_id, None)
    await _send_live_grand_result(group_id, live_state)


async def handle_live_poll_answer(pa: dict):
    """
    Handle poll answers for regular poll (type: "poll").
    Tracks user votes and calculates scores.
    """
    poll_id    = pa.get("poll_id", "")
    option_ids = pa.get("option_ids", [])
    chosen     = option_ids[0] if option_ids else None

    group_id = LIVE_POLL_MAP.get(poll_id)
    if not group_id:
        # Fallback: legacy in-memory active state
        for ch_id, st in LIVE_QUIZ_STATE.items():
            if st.get("active") and st.get("current_poll_id") == poll_id:
                group_id = ch_id
                break
    if not group_id:
        return

    state = LIVE_QUIZ_STATE.get(group_id)
    if not state or not state.get("active"):
        return

    user      = pa.get("user", {})
    uid_str   = str(user.get("id", "unknown"))
    user_name = user.get("first_name", "User")
    username  = user.get("username", "")
    now       = time.time()
    idx       = state["current_idx"]

    if "scores" not in state:
        state["scores"] = {}

    if uid_str not in state["scores"]:
        state["scores"][uid_str] = {
            "name":             user_name,
            "username":         username,
            "correct":          0,
            "wrong":            0,
            "answered_qs":      set(),
            "last_answer_time": now,
            "first_seen_time":  now,
        }

    s = state["scores"][uid_str]
    s["name"]             = user_name
    s["username"]         = username
    s["last_answer_time"] = now

    if idx in s["answered_qs"]:
        return

    s["answered_qs"].add(idx)

    correct_idx = state.get("current_correct_idx", 0)
    if chosen == correct_idx:
        s["correct"] += 1
    else:
        s["wrong"]   += 1

    # ✅ Track which option each user voted for
    if chosen is not None:
        poll_id_key = state.get("current_poll_id", "current")
        if "option_voters" not in state:
            state["option_voters"] = {}
        if poll_id_key not in state["option_voters"]:
            state["option_voters"][poll_id_key] = {0: [], 1: [], 2: [], 3: []}
        voters_for_poll = state["option_voters"][poll_id_key]
        if chosen in voters_for_poll:
            voters_for_poll[chosen].append(s["name"])


async def _send_live_grand_result(group_id, state: dict):
    try:
        from pdf_handler import get_live_motivation_and_ayat
    except Exception:
        def get_live_motivation_and_ayat(pct):
            return ("চেষ্টা চালিয়ে যাও!", "\"নিশ্চয়ই কষ্টের সাথে স্বস্তি আছে।\" — সূরা আল-ইনশিরাহ, ৯৪:৫")

    topic             = state["topic"]
    total             = state["total"]
    scores            = state.get("scores", {})
    session_id        = state["session_id"]
    quiz_start        = state["quiz_start_time"]
    participant_count = len(scores)

    score_list = []
    for uid_str, s in scores.items():
        correct  = s["correct"]
        wrong    = s["wrong"]
        answered = len(s["answered_qs"])
        skipped  = total - answered

        score = round(correct - wrong * 0.25, 2)

        total_secs = s["last_answer_time"] - quiz_start
        total_secs = max(0, total_secs)
        mins       = int(total_secs // 60)
        secs_rem   = int(total_secs % 60)
        if mins > 0:
            time_str = f"{mins}m {secs_rem:02d}s"
        else:
            time_str = f"{secs_rem}s"

        participation_pct = round(answered / total * 100, 1) if total else 0
        mark_pct          = round(correct / total * 100, 1)  if total else 0

        score_list.append({
            "user_id":            int(uid_str) if uid_str.isdigit() else 0,
            "name":               s["name"],
            "username":           s.get("username", ""),
            "correct":            correct,
            "wrong":              wrong,
            "skipped":            skipped,
            "score":              score,
            "total":              total,
            "time_str":           time_str,
            "total_secs":         total_secs,
            "participation_pct":  participation_pct,
            "mark_pct":           mark_pct,
        })

        try:
            await db_save_live_result(
                session_id,
                int(uid_str) if uid_str.isdigit() else 0,
                s["name"], correct, wrong, skipped, total,
                total_secs / answered if answered else 0
            )
        except Exception as e:
            logger.error(f"[LIVE] db_save_live_result error: {e}")

    qualified = [s for s in score_list if s["participation_pct"] >= 15]
    qualified.sort(key=lambda x: (-x["score"], x["total_secs"]))

    medals = ["🥇", "🥈", "🥉"]

    def _fmt(i: int, s: dict) -> str:
        medal  = medals[i] if i < 3 else f"{i+1}."
        uname  = f" @{s['username']}" if s.get("username") else ""
        prefix = "───────────────\n" if i == 0 else ""
        return (
            f"{prefix}"
            f"{medal} {s['name']}{uname}\n"
            f"   ✅: {s['correct']}  "
            f"❌: {s['wrong']}  "
            f"⏭ Skip: {s['skipped']}\n"
            f"   📊 Score: {s['score']} / {s['total']}  "
            f"⏱️ {s['time_str']}"
        )

    header = (
        f"🟥Grand Result of ATLAS Live Quiz\n\n"
        f"🌟Topic: {topic}\n"
        f"🎯Total MCQ: {total}\n"
        f"⚡Total Participants: {participant_count}\n\n"
        f"💎Best Scorers💎\n"
    )

    footer = (
        "\n──────────────────────────────\n"
        "🎉 Congratulations everyone! "
        "Stay ready for our next quiz."
    )

    parts        = []
    current_part = []
    current_len  = len(header)

    for i, s in enumerate(qualified):
        line     = _fmt(i, s)
        line_len = len(line) + 1

        if current_len + line_len > 3800:
            parts.append(current_part)
            current_part = [line]
            current_len  = line_len
        else:
            current_part.append(line)
            current_len += line_len

    if current_part:
        parts.append(current_part)

    if not parts:
        await tg_post("sendMessage", {
            "chat_id": group_id,
            "text":    header +
                       "\n(কেউ ১৫% বা তার বেশি প্রশ্নের উত্তর দেননি)" +
                       footer
        })
    else:
        for i, part in enumerate(parts):
            is_last = (i == len(parts) - 1)
            if i == 0:
                text = header + "\n".join(part)
            else:
                text = f"🚀Grand Result — Part {i+1}\n\n" + "\n".join(part)
            if is_last:
                text += footer

            await tg_post("sendMessage", {
                "chat_id": group_id,
                "text":    text
            })
            await asyncio.sleep(0.5)

    bd_time = _get_bd_time()

    for s in score_list:
        if s["participation_pct"] >= 70 and s["mark_pct"] < 50 and s["user_id"]:
            motivation, ayat = get_live_motivation_and_ayat(s["mark_pct"])

            dm_text = (
                f"আসসালামু আলাইকুম, প্রিয় শিক্ষার্থী {s['name']}!\n\n"
                f"আপনি ({bd_time}) তে {topic} এ এটলাসের লাইভ কুইজে "
                f"অংশগ্রহণ করেছিলেন। হয়তোবা কিছু ঘাটতির কারণে আপনার "
                f"বেস্ট রেজাল্ট পাননি, টেনশন করবেন না, পরবর্তী কুইজে "
                f"আরো ভালো প্রস্তুতি নিয়ে কুইজ দিবেন, এটলাস টিম "
                f"আশাবাদী আপনি আরো ভালো করবেন।\n\n"
                f"{motivation}\n\n"
                f"{ayat}\n\n"
                f"✅শুভকামনায়-Team ATLAS\n\n"
                f"⚙️পরবর্তী কুইজে অংশগ্রহণ করতে যুক্ত থাকুন "
                f"এটলাসের সাথেই।\n\n"
                f"🌟লাইভ কুইজ গ্রুপ:\n"
                f"https://t.me/LiveQuizByAtlas\n"
                f"🌟এটলাসের সকল গ্রুপ+চ্যানেল:\n"
                f"https://t.me/addlist/GECHwfEIZ_ozZmVl\n"
                f"📌ATLAS Website: Atlascourses.com"
            )
            try:
                await send_msg(s["user_id"], dm_text)
                await asyncio.sleep(0.5)
            except Exception:
                pass

# ============================================================
# FEATURE: /pdfc image collection → /done → PDF
# ============================================================
async def handle_pdf_image_mode(msg: dict):
    """
    /pdfc দিলে bot image চাইবে।
    User একটার পর একটা image পাঠাবে।
    /done দিলে সব image দিয়ে ATLAS.pdf বানাবে।
    """
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text","").strip()

    if text in ("/pdfc", "/pdf_collect"):
        IMG_COLLECTION[uid] = {"imgs": [], "collecting": True, "chat_id": chat_id}
        await send_msg(chat_id,
            "📸 Image collection mode চালু!\n\n"
            "একটার পর একটা image পাঠাও।\n"
            "শেষ হলে /done দাও — ATLAS.pdf বানিয়ে দেব।\n\n"
            "❌ বাতিল করতে /cancel দাও।"
        )
        return

    if text == "/done":
        if uid not in IMG_COLLECTION or not IMG_COLLECTION[uid].get("collecting"):
            await send_msg(chat_id, "❌ আগে /pdfc দিয়ে image collection শুরু করো!")
            return
        imgs = IMG_COLLECTION[uid].get("imgs",[])
        if not imgs:
            await send_msg(chat_id, "❌ কোনো image পাওয়া যায়নি!")
            return

        loading = await send_msg(chat_id, f"⏳ {len(imgs)} টি image দিয়ে PDF বানানো হচ্ছে...")
        IMG_COLLECTION.pop(uid, None)

        try:
            from PIL import Image as PILImage
            import io as _io
            pdf_images = []
            for img_bytes in imgs:
                im = PILImage.open(_io.BytesIO(img_bytes)).convert("RGB")
                pdf_images.append(im)

            buf = _io.BytesIO()
            pdf_images[0].save(buf, format="PDF", save_all=True, append_images=pdf_images[1:])
            pdf_bytes = buf.getvalue()

            await send_document(chat_id, pdf_bytes, "ATLAS.pdf",
                caption=f"📄 ATLAS.pdf — {len(pdf_images)} pages",
                mime_type="application/pdf")

            loading_id = loading.get("result",{}).get("message_id")
            if loading_id:
                await edit_msg(chat_id, loading_id,
                    f"✅ ATLAS.pdf তৈরি হয়েছে! ({len(pdf_images)} pages)")

        except Exception as e:
            await send_msg(chat_id, f"❌ PDF বানাতে error: {e}")
        return

    if text == "/cancel":
        IMG_COLLECTION.pop(uid, None)
        if uid in RAPID_PENDING:
            RAPID_PENDING.pop(uid, None)
            await send_msg(chat_id, "❌ /rapid scheduling বাতিল।")
            return
        await send_msg(chat_id, "❌ Image collection বাতিল।")
        return

async def handle_incoming_image_for_collection(msg: dict):
    """User যদি image collection mode-এ থাকে, image save করো"""
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]

    if uid not in IMG_COLLECTION or not IMG_COLLECTION[uid].get("collecting"):
        return False

    photo = msg.get("photo") or (msg.get("document") if msg.get("document",{}).get("mime_type","").startswith("image") else None)
    if not photo:
        return False

    if isinstance(photo, list):
        file_id = photo[-1]["file_id"]
    else:
        file_id = photo["file_id"]

    try:
        img_bytes = await download_tg_file(file_id)
        IMG_COLLECTION[uid]["imgs"].append(img_bytes)
        count = len(IMG_COLLECTION[uid]["imgs"])
        await send_msg(chat_id, f"✅ Image {count} save হয়েছে! (আরো দাও বা /done)")
    except Exception as e:
        await send_msg(chat_id, f"❌ Image save error: {e}")
    return True

# ============================================================
# FEATURE 10: POLL FLOW
# ============================================================
def _poll_end_kb(cache_id: str, cache: dict) -> dict:
    kb = {"inline_keyboard": [
        [{"text": "🔄 Again Practice", "callback_data": f"pollagain_{cache_id}"}],
        [{"text": "🆕 New Poll (নতুন MCQ)", "callback_data": f"pollnew_{cache_id}"}]
    ]}
    back_url = build_back_url(cache.get("channel_id", ""), source_msg_id(cache))
    if back_url:
        kb["inline_keyboard"].append([{"text": "↩️ Back to Source", "url": back_url}])
    return kb

async def handle_poll_again(cache_id: str, user: dict, chat_id: int):
    settings = await db_get_settings()
    tag = settings.get("tag", "")
    exp_footer = settings.get("exp_footer", "")
    cache = await db_get_mcq_cache(cache_id)
    if not cache:
        await send_msg(chat_id, "❌ Cache পাওয়া যায়নি!")
        return

    mcqs = cache["mcq_data"]
    topic = cache["topic"]
    page = cache["page_number"]
    total = len(mcqs)

    pre_caption = (
        f"🔄 <b>Poll Practice শুরু হচ্ছে!</b>\n\n"
        f"🌟 Topic: {topic}\n📄 Page No: {fmt_page(page)}\n📝 Total MCQ: {total}\n\n⏱️ Are you ready?"
    )
    img_id = cache.get("image_file_id")
    if img_id:
        r = await send_photo_by_id(chat_id, img_id, pre_caption, parse_mode="HTML")
        if not r.get("ok"):
            await send_msg(chat_id, pre_caption, parse_mode="HTML")
    else:
        await send_msg(chat_id, pre_caption)

    await send_msg(chat_id, "3️⃣ 2️⃣ 1️⃣ 🚀 শুরু!")
    await asyncio.sleep(1)

    for i, mcq in enumerate(mcqs):
        opts = mcq.get("options", [])
        ans_idx = {"A": 0, "B": 1, "C": 2, "D": 3}.get(mcq.get("answer", "A"), 0)
        q_text = f"({i+1}/{total}) {mcq['question']}"
        if tag:
            q_text = f"{tag}\n\n{q_text}"
        exp = mcq.get("explanation", "")
        if exp_footer:
            exp = f"{exp}\n{exp_footer}"
        await send_poll(chat_id, q_text, opts, ans_idx, explanation=exp[:200])
        await asyncio.sleep(1.5)

    end_text = (
        f"✅ <b>Poll শেষ!</b>\n\n🎯 Topic: {topic}\n📄 Page: {fmt_page(page)}\n"
        f"📝 {total} টি poll পাঠানো হয়েছে!\n\n🔄 আবার practice করতে বা নতুন poll চাইলে নিচের বাটন চাপো।"
    )
    end_kb = _poll_end_kb(cache_id, cache)
    if img_id:
        end_r = await send_photo_by_id(chat_id, img_id, end_text[:1024], parse_mode="HTML")
        if end_r.get("ok"):
            await send_msg(chat_id, "⬇️ আবার practice করতে:", reply_markup=end_kb)
        else:
            await send_msg(chat_id, end_text, reply_markup=end_kb)
    else:
        await send_msg(chat_id, end_text, reply_markup=end_kb)

# ============================================================
# FEATURE 11: POLL NEW
# ============================================================
async def handle_poll_new(cache_id: str, user: dict, chat_id: int, msg_id: int = None):
    uid = user["id"]
    count = await db_get_new_gen_count(cache_id, uid)
    if count >= 5:
        await send_msg(chat_id, "❌ Maximum 5 বার নতুন MCQ বানানো যাবে!")
        return
    cache = await db_get_mcq_cache(cache_id)
    if not cache:
        await send_msg(chat_id, "❌ Cache পাওয়া যায়নি!")
        return
    topic = cache["topic"]
    page = cache["page_number"]
    channel_id = cache.get("channel_id", "")
    image_msg_id = cache.get("image_msg_id")
    image_file_id = cache.get("image_file_id")
    if not image_file_id:
        await send_msg(chat_id, "❌ Original image পাওয়া যায়নি!")
        return

    eta = 30
    loading_msg = await send_msg(chat_id, f"New Poll বানানো হচ্ছে\nঅনুমানিত সময়: {eta}s\n[░░░░░░░░░░ 0%]\n{eta}s বাকি...")
    loading_id = loading_msg.get("result", {}).get("message_id")

    async def update_progress():
        for pct in [20, 40, 60, 80]:
            await asyncio.sleep(eta * 0.2)
            bars = "█" * (pct // 10) + "░" * (10 - pct // 10)
            remaining = int(eta * (1 - pct / 100))
            if loading_id:
                await edit_msg(chat_id, loading_id, f"New Poll বানানো হচ্ছে\nঅনুমানিত সময়: {eta}s\n[{bars} {pct}%]\n{remaining}s বাকি...")

    img_bytes = await download_tg_file(image_file_id)
    from PIL import Image as PILImage
    img = PILImage.open(BytesIO(img_bytes))

    progress_task = asyncio.create_task(update_progress())
    new_mcqs = await generate_new_mcq(img, topic, page, count=15)
    progress_task.cancel()

    if not new_mcqs:
        await send_msg(chat_id, "❌ MCQ generate হয়নি!")
        return

    await db_increment_gen_count(cache_id, uid)
    if loading_id:
        await edit_msg(chat_id, loading_id, f"✅ {len(new_mcqs)} টি নতুন MCQ ready!\n\nশুরু হচ্ছে...")

    new_cache_id = gen_session_id()
    await db_save_mcq_cache(new_cache_id, new_cache_id, page, topic, new_mcqs, [],
                            image_file_id, image_msg_id, channel_id,
                            is_new_gen=True, end_msg_id=cache.get("end_msg_id"))

    settings = await db_get_settings()
    tag = settings.get("tag", "")
    exp_footer = settings.get("exp_footer", "")
    new_cache = await db_get_mcq_cache(new_cache_id)
    total = len(new_mcqs)

    pre_caption = (
        f"🆕 <b>New Poll শুরু হচ্ছে!</b>\n\n"
        f"🌟 Topic: {topic}\n📄 Page No: {fmt_page(page)}\n📝 Total MCQ: {total}\n\n⏱️ Are you ready?"
    )
    if image_file_id:
        r = await send_photo_by_id(chat_id, image_file_id, pre_caption, parse_mode="HTML")
        if not r.get("ok"):
            await send_msg(chat_id, pre_caption, parse_mode="HTML")
    else:
        await send_msg(chat_id, pre_caption)

    await send_msg(chat_id, "3️⃣ 2️⃣ 1️⃣ 🚀 শুরু!")
    await asyncio.sleep(1)

    for i, mcq in enumerate(new_mcqs):
        opts = mcq.get("options", [])
        ans_idx = {"A": 0, "B": 1, "C": 2, "D": 3}.get(mcq.get("answer", "A"), 0)
        q_text = f"({i+1}/{total}) {mcq['question']}"
        if tag:
            q_text = f"{tag}\n\n{q_text}"
        exp = mcq.get("explanation", "")
        if exp_footer:
            exp = f"{exp}\n{exp_footer}"
        await send_poll(chat_id, q_text, opts, ans_idx, explanation=exp[:200])
        await asyncio.sleep(1.5)

    remaining_new = 5 - (count + 1)
    kb = _poll_end_kb(new_cache_id, new_cache or cache)
    kb["inline_keyboard"][1][0]["text"] = f"🆕 New Poll ({remaining_new} বাকি)"

    end_text = (
        f"✅ <b>New Poll শেষ!</b>\n\n🎯 Topic: {topic}\n📄 Page: {fmt_page(page)}\n"
        f"📝 {total} টি poll পাঠানো হয়েছে!\n🔢 আর {remaining_new} বার নতুন poll বানানো যাবে।"
    )
    if image_file_id:
        end_r = await send_photo_by_id(chat_id, image_file_id, end_text[:1024], parse_mode="HTML")
        if end_r.get("ok"):
            await send_msg(chat_id, "⬇️ পরবর্তী পদক্ষেপ:", reply_markup=kb)
        else:
            await send_msg(chat_id, end_text, reply_markup=kb)
    else:
        await send_msg(chat_id, end_text, reply_markup=kb)

# ============================================================
# FEATURE 12: SEQUENTIAL QUIZ ENGINE
# ============================================================
QUIZ_STATE = {}
LAST_QUIZ = {}

async def qs_set(uid: int, state: dict):
    QUIZ_STATE[uid] = state
    state_copy = {k: v for k, v in state.items() if k != "timer_task"}
    asyncio.create_task(d1_set(f"qs_{uid}", state_copy, ttl=3600))

async def qs_get(uid: int) -> dict:
    if uid in QUIZ_STATE:
        return QUIZ_STATE[uid]
    val = await d1_get(f"qs_{uid}")
    if val and not isinstance(val, dict):
        logger.warning(f"[QS] qs_get returned non-dict for uid={uid}: {type(val)} — discarding")
        val = None
    if val:
        QUIZ_STATE[uid] = val
    return val

async def qs_del(uid: int):
    QUIZ_STATE.pop(uid, None)
    asyncio.create_task(d1_del(f"qs_{uid}"))

async def lq_set(uid: int, state: dict):
    LAST_QUIZ[uid] = state
    state_copy = {k: v for k, v in state.items() if k != "timer_task"}
    asyncio.create_task(d1_set(f"lq_{uid}", state_copy, ttl=86400))

async def lq_get(uid: int) -> dict:
    if uid in LAST_QUIZ:
        return LAST_QUIZ[uid]
    val = await d1_get(f"lq_{uid}")
    if val:
        LAST_QUIZ[uid] = val
    return val

async def start_sequential_quiz(chat_id: int, uid: int, uname: str,
                                cache_id: str, indices: list = None,
                                mode: str = "quiz", title: str = "🎯 <b>Quiz শুরু হচ্ছে!</b>"):
    cache = await db_get_mcq_cache(cache_id)
    if not cache:
        await send_msg(chat_id, "❌ Quiz পাওয়া যায়নি! Link টা সঠিক কিনা দেখো।")
        return

    all_mcqs = cache["mcq_data"]
    mcqs = [all_mcqs[i] for i in indices if i < len(all_mcqs)] if indices is not None else all_mcqs

    if not mcqs:
        await send_msg(chat_id, "✅ এই ক্যাটেগরিতে কোনো প্রশ্ন নেই!")
        return

    settings = await db_get_settings()
    await qs_del(uid)

    state = {
        "cache_id": cache_id, "mcqs": mcqs,
        "topic": cache["topic"], "page": cache["page_number"],
        "idx": 0, "right": 0, "wrong": 0, "skip": 0,
        "wrong_idx": [], "skip_idx": [], "src_indices": indices,
        "chat_id": chat_id, "uname": uname,
        "tag": settings.get("tag", ""), "exp_footer": settings.get("exp_footer", ""),
        "channel_id": cache.get("channel_id", ""), "back_msg_id": source_msg_id(cache),
        "is_new_gen": bool(cache.get("is_new_gen")), "mode": mode,
        "start": time.time(), "poll_id": None, "answered": False, "timer_task": None
    }
    await qs_set(uid, state)

    topic = cache["topic"]
    page = cache["page_number"]
    total = len(mcqs)

    pre_caption = (
        f"{title}\n\n🌟 Topic: {topic}\n📄 Page No: {fmt_page(page)}\n"
        f"📝 Total MCQ: {total}\n⏱️ প্রতিটা প্রশ্নে {QUIZ_Q_SEC} সেকেন্ড সময়\n\nপ্রস্তুত থাকো!"
    )
    img_id = cache.get("image_file_id")
    if img_id:
        r = await send_photo_by_id(chat_id, img_id, pre_caption, parse_mode="HTML")
        if not r.get("ok"):
            await send_msg(chat_id, pre_caption, parse_mode="HTML")
    else:
        await send_msg(chat_id, pre_caption, parse_mode="HTML")

    await send_msg(chat_id, "3️⃣")
    await asyncio.sleep(0.5)
    await send_msg(chat_id, "2️⃣")
    await asyncio.sleep(0.5)
    await send_msg(chat_id, "1️⃣ 🚀")
    await asyncio.sleep(0.5)
    await _send_quiz_question(uid)

async def _send_quiz_question(uid: int):
    st = await qs_get(uid)
    if not st:
        return
    st["timer_task"] = None

    i = st["idx"]
    mcq = st["mcqs"][i]
    opts = mcq.get("options", [])
    ans_idx = {"A": 0, "B": 1, "C": 2, "D": 3}.get(mcq.get("answer", "A"), 0)
    total = len(st["mcqs"])

    q_text = f"({i+1}/{total}) {mcq['question']}"
    if st["tag"]:
        q_text = f"{st['tag']}\n\n{q_text}"

    exp = mcq.get("explanation", "")
    if st["exp_footer"]:
        exp = f"{exp}\n{st['exp_footer']}"

    poll_r = await tg_post("sendPoll", {
        "chat_id": st["chat_id"],
        "question": q_text[:300],
        "options": [o[:100] for o in opts],
        "type": "quiz",
        "correct_option_id": ans_idx,
        "is_anonymous": False,
        "explanation": exp[:200],
        "open_period": QUIZ_Q_SEC
    })

    if not poll_r.get("ok"):
        st["idx"] += 1
        if st["idx"] >= len(st["mcqs"]):
            await _finish_quiz(uid)
        else:
            await _send_quiz_question(uid)
        return

    st["poll_id"] = poll_r["result"]["poll"]["id"]
    st["answered"] = False
    st["timer_task"] = None
    await qs_set(uid, st)
    st["timer_task"] = asyncio.create_task(_quiz_timeout(uid, st["poll_id"]))

async def _quiz_timeout(uid: int, poll_id: str):
    try:
        await asyncio.sleep(QUIZ_Q_SEC + 0.1)
        st = await qs_get(uid)
        if not st:
            return
        st["timer_task"] = None
        if st and st["poll_id"] == poll_id and not st["answered"]:
            st["answered"] = True
            st["skip"] += 1
            st["skip_idx"].append(st["idx"])
            await qs_set(uid, st)
            await asyncio.sleep(0.1)
            await _advance_quiz(uid)
    except asyncio.CancelledError:
        pass

async def _advance_quiz(uid: int):
    st = await qs_get(uid)
    if not st:
        return
    st["timer_task"] = None
    st["idx"] += 1
    if st["idx"] >= len(st["mcqs"]):
        await _finish_quiz(uid)
    else:
        await _send_quiz_question(uid)

async def handle_poll_answer(pa: dict):
    try:
        # Live quiz check FIRST (v4) — poll_id based routing
        poll_id_ck = pa.get("poll_id", "")
        if poll_id_ck and poll_id_ck in LIVE_POLL_MAP:
            await handle_live_poll_answer(pa)
            return
        # Fallback: legacy active-state check
        if LIVE_QUIZ_STATE:
            try:
                await handle_live_poll_answer(pa)
            except Exception:
                pass

        # D1 quiz system check
        uid_ck = pa.get("user", {}).get("id")
        if uid_ck and uid_ck in QUIZ_SESSIONS:
            await handle_quiz_poll_answer(pa)
            return

        uid = pa["user"]["id"]
        st = await qs_get(uid)
        if not st or not isinstance(st, dict) or pa.get("poll_id") != st.get("poll_id") or st.get("answered"):
            return

        st["answered"] = True
        if st.get("timer_task"):
            st["timer_task"].cancel()

        mcq = st["mcqs"][st["idx"]]
        ci = {"A": 0, "B": 1, "C": 2, "D": 3}.get(mcq.get("answer", "A"), 0)
        option_ids = pa.get("option_ids", [])
        chosen = option_ids[0] if option_ids else None

        if chosen == ci:
            st["right"] += 1
        else:
            st["wrong"] += 1
            st["wrong_idx"].append(st["idx"])

        await qs_set(uid, st)
        await asyncio.sleep(0.1)
        await _advance_quiz(uid)
    except Exception as e:
        logger.error(f"[PollAnswer] {e}")

async def _finish_quiz(uid: int):
    st = await qs_get(uid)
    await qs_del(uid)
    if st:
        st["timer_task"] = None
    if not st:
        return
    if st.get("timer_task"):
        st["timer_task"].cancel()

    await lq_set(uid, st)
    await db_save_last_quiz(uid, st)

    chat_id = st["chat_id"]
    cache_id = st["cache_id"]
    total = len(st["mcqs"])
    right, wrong, skipped = st["right"], st["wrong"], st["skip"]
    neg = round(wrong * 0.25, 2)
    fin = round(right - neg, 2)
    pct = round(right / total * 100) if total else 0
    elapsed = int(time.time() - st["start"])
    mins, secs = divmod(elapsed, 60)

    if pct >= 80:
        grade = "🏆 অসাধারণ!অনেক ভালো করেছো,প্রিয় শিক্ষার্থী!--রাফি ভাইয়া(এটলাস)"
    elif pct >= 60:
        grade = "✅মোটামুটি ভালো করেছ!চেষ্টা চালিয়ে যাও😊--রাফি ভাইয়া(এটলাস)"
    elif pct >= 40:
        grade = "📚 আরো পড়তে হবে!হাল ছেড়ো না!✊-রাফি ভাইয়া(এটলাস)"
    else:
        grade = "💪 পড়া হয়নি!হাল ছেড়ো না!আবার পড়ে প্রাক্টিস করো-শুভকামনায়--রাফি ভাইয়া(এটলাস)"

    if not st["is_new_gen"] and st["mode"] == "quiz":
        await db_save_leaderboard(cache_id, uid, st["uname"], st["topic"], st["page"], right, total, fin)

    ayat = get_random_ayat()
    motivation = get_motivation(pct)

    result_caption = (
        f"🎯 <b>QUIZ COMPLETE!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌟 Topic: {st['topic']}\n"
        f"📄 Page No: {fmt_page(st['page'])}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 Total: {total}\n"
        f"✅ সঠিক: {right}\n"
        f"❌ ভুল: {wrong}\n"
        f"⏭️ Skip (time out): {skipped}\n"
        f"📊 Negative: -{neg} ({wrong}×0.25)\n"
        f"🏆 Final Score: {fin}/{total}\n"
        f"📈 Percentage: {pct}%\n"
        f"⏱️ সময় লেগেছে: {mins}:{secs:02d}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📖 {ayat}"
    )

    motivation_text = f"\n{grade}\n\n{motivation}"

    exam_url = f"{HF_SPACE_URL}/exam/{cache_id}?uid={uid}&name={st['uname']}"
    back_url = build_back_url(st["channel_id"], st["back_msg_id"])
    wrong_count = len(st["wrong_idx"])
    skip_count = len(st["skip_idx"])
    special_count = len(set(st["wrong_idx"] + st["skip_idx"]))

    kb = {"inline_keyboard": []}
    kb["inline_keyboard"].append([{"text": "🆕 New Quiz (নতুন MCQ)", "callback_data": f"qnew_{cache_id}"}])
    if wrong_count > 0:
        kb["inline_keyboard"].append([{"text": f"❌ Mistake Practice ({wrong_count} টি ভুল)", "callback_data": "qmis"}])
    if special_count > 0:
        kb["inline_keyboard"].append([{"text": f"🔥 Special Practice ({special_count} টি wrong+skip)", "callback_data": "qspe"}])
    kb["inline_keyboard"].append([{"text": "🌐 Website Exam দাও", "url": exam_url}])
    if not st["is_new_gen"] and st["mode"] == "quiz":
        kb["inline_keyboard"].append([{"text": "🏆 Leaderboard দেখো", "callback_data": f"polllb_{cache_id}"}])
    if back_url:
        kb["inline_keyboard"].append([{"text": "↩️ Back to Source", "url": back_url}])
    kb["inline_keyboard"].append([{"text": "🔄 Poll হিসেবে আবার দেখো", "callback_data": f"pollagain_{cache_id}"}])

    cache = await db_get_mcq_cache(cache_id)
    img_id = cache.get("image_file_id") if cache else None

    if img_id:
        caption_trimmed = result_caption[:1024]
        await send_photo_by_id(chat_id, img_id, caption_trimmed, parse_mode="HTML")
        await send_msg(chat_id, motivation_text, reply_markup=kb)
    else:
        full_result = result_caption + "\n" + motivation_text
        await send_msg(chat_id, full_result, reply_markup=kb)

async def handle_quiz_solve(msg: dict, cache_id: str):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    uname = msg["from"].get("username") or msg["from"].get("first_name", "User")
    await db_track_user(uid, uname)
    await start_sequential_quiz(chat_id, uid, uname, cache_id)

# ============================================================
# NEW QUIZ
# ============================================================
async def handle_quiz_new(cache_id: str, user: dict, chat_id: int):
    uid = user["id"]
    uname = user.get("username") or user.get("first_name", "User")
    count = await db_get_new_gen_count(cache_id, uid)
    if count >= 5:
        await send_msg(chat_id, "❌ Maximum 5 বার নতুন Quiz বানানো যাবে!")
        return
    cache = await db_get_mcq_cache(cache_id)
    if not cache:
        await send_msg(chat_id, "❌ Cache পাওয়া যায়নি!")
        return
    image_file_id = cache.get("image_file_id")
    if not image_file_id:
        await send_msg(chat_id, "❌ Original image পাওয়া যায়নি!")
        return
    loading = await send_msg(chat_id, "⏳ নতুন MCQ তৈরি হচ্ছে... (~30s)")
    loading_id = loading.get("result", {}).get("message_id")
    img_bytes = await download_tg_file(image_file_id)
    from PIL import Image as PILImage
    img = PILImage.open(BytesIO(img_bytes))
    new_mcqs = await generate_new_mcq(img, cache["topic"], cache["page_number"], count=15)
    if not new_mcqs:
        await send_msg(chat_id, "❌ MCQ generate হয়নি!")
        return
    await db_increment_gen_count(cache_id, uid)
    new_cache_id = gen_session_id()
    await db_save_mcq_cache(new_cache_id, new_cache_id, cache["page_number"], cache["topic"],
                            new_mcqs, [], image_file_id, cache.get("image_msg_id"),
                            cache.get("channel_id"), is_new_gen=True, end_msg_id=cache.get("end_msg_id"))
    if loading_id:
        await edit_msg(chat_id, loading_id, f"✅ {len(new_mcqs)} টি নতুন MCQ ready!")
    await start_sequential_quiz(chat_id, uid, uname, new_cache_id, title="🆕 <b>New Quiz শুরু হচ্ছে!</b>")

# ============================================================
# MISTAKE / SPECIAL PRACTICE
# ============================================================
async def handle_quiz_practice(uid: int, chat_id: int, uname: str, kind: str):
    last = await lq_get(uid)
    if not last:
        last = await db_get_last_quiz(uid)
    if not last:
        await send_msg(chat_id, "❌ কোনো quiz history পাওয়া যায়নি!\nআগে একটা quiz শেষ করো।")
        return

    if kind == "mis":
        indices = list(last["wrong_idx"])
        title = "❌ <b>Mistake Practice শুরু হচ্ছে!</b>"
        if not indices:
            await send_msg(chat_id, "🎉 কোনো ভুল নেই — দারুণ পারফরম্যান্স!")
            return
    else:
        indices = sorted(set(last["wrong_idx"] + last["skip_idx"]))
        title = "🔥 <b>Special Practice শুরু হচ্ছে!</b>"
        if not indices:
            await send_msg(chat_id, "🎉 কোনো ভুল বা skip নেই — পারফেক্ট!")
            return

    src = last.get("src_indices")
    if src is not None:
        indices = [src[i] for i in indices if i < len(src)]

    count = len(indices)
    await send_msg(chat_id, f"📝 {count} টি প্রশ্ন নিয়ে practice শুরু হচ্ছে...")
    await start_sequential_quiz(chat_id, uid, uname, last["cache_id"], indices=indices, mode="practice", title=title)



async def handle_collect_command(msg: dict):
    """Poll collection from forwarded polls"""
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text", "").strip()

    if text == "/collect":
        await send_msg(chat_id,
            "📊 Poll collection started!\n\n"
            "Forward polls to collect.\n"
            "/cstatus — check count\n"
            "/cdone — download CSV\n"
            "/ccancel — clear"
        )
        return
    if text == "/cstatus":
        rows = await d1_select(
            "SELECT COUNT(*) as c FROM poll_collection WHERE user_id=?1", [uid]
        )
        await send_msg(chat_id, f"📊 Total collected: {rows[0]['c'] if rows else 0} polls")
        return
    if text == "/cdone":
        rows = await d1_select(
            "SELECT poll_data FROM poll_collection WHERE user_id=?1", [uid]
        )
        if not rows:
            await send_msg(chat_id, "❌ No polls collected!")
            return
        import io as _io, csv as _csv
        buf = _io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(["questions","option1","option2","option3","option4","answer","explanation","type","section"])
        for r in rows:
            pd = json.loads(r["poll_data"])
            opts = pd.get("options", [])
            while len(opts) < 4:
                opts.append("")
            ans = str((pd.get("correct", 0) or 0) + 1)
            writer.writerow([pd.get("question",""), opts[0], opts[1], opts[2], opts[3],
                             ans, pd.get("explanation",""), "1", "1"])
        await send_document(chat_id, buf.getvalue().encode("utf-8"),
            f"collected_{len(rows)}.csv",
            caption=f"✅ {len(rows)} polls collected!",
            mime_type="text/csv")
        await d1_run("DELETE FROM poll_collection WHERE user_id=?1", [uid])
        return
    if text == "/ccancel":
        await d1_run("DELETE FROM poll_collection WHERE user_id=?1", [uid])
        await send_msg(chat_id, "❌ Collection cancelled!")
        return


async def handle_poll_auto_collect(msg: dict):
    """Auto-collect forwarded polls"""
    poll = msg.get("poll")
    if not poll or not msg.get("forward_date"):
        return False
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]
    try:
        poll_data = {
            "question": poll.get("question", ""),
            "options": [o.get("text", "") for o in poll.get("options", [])],
            "correct": poll.get("correct_option_id"),
            "explanation": poll.get("explanation", "")
        }
        await d1_run(
            "INSERT INTO poll_collection (user_id, poll_data) VALUES (?1, ?2)",
            [uid, json.dumps(poll_data)]
        )
        rows = await d1_select("SELECT COUNT(*) as c FROM poll_collection WHERE user_id=?1", [uid])
        await send_msg(chat_id, f"📊 Collected! Total: {rows[0]['c'] if rows else 0} polls")
        return True
    except Exception as e:
        logger.error(f"[Collect] Error: {e}")
    return False


async def handle_merge_command(msg: dict):
    """CSV file merge"""
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text", "").strip()
    reply = msg.get("reply_to_message")

    args = text.replace("/merge", "").strip()

    if args == "done":
        row = sb.table("quiz_sessions").select("data").eq("key", f"merge_{uid}").execute()
        if not row.data:
            await send_msg(chat_id, "❌ No files to merge!")
            return
        merge_data = json.loads(row.data[0]["data"])
        files = merge_data.get("files", [])
        if not files:
            await send_msg(chat_id, "❌ No files to merge!")
            return
        all_rows = []
        header = None
        for content in files:
            lines = [l for l in content.split("\n") if l.strip()]
            if not header:
                header = lines[0]
                all_rows.append(header)
            all_rows.extend(lines[1:])
        merged = "\n".join(all_rows)
        await send_document(chat_id, merged.encode("utf-8"),
            f"merged_{len(all_rows)-1}.csv",
            caption=f"✅ Merged: {len(all_rows)-1} rows from {len(files)} files",
            mime_type="text/csv")
        sb.table("quiz_sessions").delete().eq("key", f"merge_{uid}").execute()
        return

    if args == "status":
        row = sb.table("quiz_sessions").select("data").eq("key", f"merge_{uid}").execute()
        count = len(json.loads(row.data[0]["data"]).get("files", [])) if row.data else 0
        await send_msg(chat_id, f"📊 Total files: {count}")
        return

    if args == "cancel":
        sb.table("quiz_sessions").delete().eq("key", f"merge_{uid}").execute()
        await send_msg(chat_id, "❌ Merge cancelled!")
        return

    if reply and reply.get("document"):
        try:
            csv_bytes = await download_tg_file(reply["document"]["file_id"])
            content = csv_bytes.decode("utf-8-sig")
            row = sb.table("quiz_sessions").select("data").eq("key", f"merge_{uid}").execute()
            files = json.loads(row.data[0]["data"]).get("files", []) if row.data else []
            files.append(content)
            sb.table("quiz_sessions").upsert({
                "key": f"merge_{uid}",
                "data": json.dumps({"files": files}),
                "updated_at": int(time.time())
            }).execute()
            await send_msg(chat_id, f"📎 File {len(files)} received! Total: {len(files)}\n/merge done when ready")
        except Exception as e:
            await send_msg(chat_id, f"❌ Error: {e}")
        return

    await send_msg(chat_id,
        "🔗 CSV ফাইলে reply করে /merge দাও\n"
        "/merge done — merge করো\n"
        "/merge status — count দেখো\n"
        "/merge cancel — বাতিল"
    )


async def handle_error_command(msg: dict):
    """Owner/Admin only — সাম্প্রতিক bot error/crash গুলো clearly দেখায়
    (file, line number, function, message সহ) যাতে AI/dev দ্রুত debug করতে পারে।"""
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text", "").strip()

    if not await db_is_owner_or_admin(uid):
        await send_msg(chat_id, "❌ Owner/Admin only!")
        return

    if text.strip() in ("/error clear", "/errors clear"):
        await clear_error_logs()
        await send_msg(chat_id, "✅ Error log clear করা হয়েছে!")
        return

    parts = text.split()
    limit = 10
    if len(parts) > 1 and parts[1].isdigit():
        limit = min(int(parts[1]), 30)

    errors = await get_recent_errors(limit)
    if not errors:
        await send_msg(chat_id, "✅ কোনো error পাওয়া যায়নি! Bot ক্লিন আছে।")
        return

    import html as _html
    lines = [f"🛑 <b>সাম্প্রতিক {len(errors)}টি Error</b>\n"]
    for i, e in enumerate(errors, 1):
        ts = e.get("created_at")
        when = datetime.fromtimestamp(ts, pytz.timezone("Asia/Dhaka")).strftime("%d-%b %I:%M %p") if ts else "N/A"
        fname = _html.escape((e.get("filename") or "?").split("/")[-1])
        lineno = e.get("lineno") or "?"
        func = _html.escape(e.get("funcname") or "?")
        message = _html.escape((e.get("message") or "")[:300])
        lines.append(
            f"<b>{i}.</b> 📄 <code>{fname}:{lineno}</code> — <code>{func}()</code>\n"
            f"🕐 {when}\n"
            f"💬 {message}\n"
        )

    full_text = "\n".join(lines)
    # Telegram message limit safety — split if too long
    if len(full_text) > 3800:
        full_text = full_text[:3800] + "\n\n…(আরও আছে, /error 5 দিয়ে কম দেখাও)"

    await send_msg(chat_id, full_text)


async def handle_watermark_command(msg: dict):
    """v1.2: /watermark — ask the user to send a PDF, then ask for watermark
    text, then return the watermarked PDF. Ported from AtlasMasterBot."""
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]
    WATERMARK_PENDING[uid] = {"step": "awaiting_pdf"}
    await send_msg(chat_id, "📄 যে PDF-এ watermark বসাতে চান, সেটা পাঠান।")

async def handle_watermark_document(msg: dict) -> bool:
    """Called from the document/text router. Returns True if the message
    was consumed by the watermark flow, False otherwise (so other PDF
    handlers can still process it)."""
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]
    state = WATERMARK_PENDING.get(uid)
    if not state:
        return False

    if state.get("step") == "awaiting_pdf":
        doc = msg.get("document")
        if not doc or not (doc.get("file_name") or "").lower().endswith(".pdf"):
            await send_msg(chat_id, "❌ দয়া করে একটি PDF file পাঠান।")
            return True
        try:
            pdf_bytes = await download_tg_file(doc["file_id"])
        except Exception as e:
            logger.error(f"[Watermark] download error: {e}")
            await send_msg(chat_id, f"❌ PDF download করতে সমস্যা হয়েছে: {e}")
            WATERMARK_PENDING.pop(uid, None)
            return True
        state["pdf_bytes"] = pdf_bytes
        state["step"] = "awaiting_text"
        await send_msg(chat_id, "✏️ Watermark-এ কী লেখা থাকবে? (যেমন: তোমার নাম/চ্যানেল)")
        return True

    if state.get("step") == "awaiting_text":
        text = (msg.get("text") or "").strip()
        if not text:
            await send_msg(chat_id, "❌ দয়া করে watermark text লিখুন।")
            return True
        pdf_bytes = state.get("pdf_bytes")
        WATERMARK_PENDING.pop(uid, None)
        loading = await send_msg(chat_id, "⏳ Watermark বসানো হচ্ছে...")
        try:
            watermarked = add_watermark_to_pdf(pdf_bytes, text)
            await send_document(chat_id, watermarked, filename="watermarked.pdf", caption="✅ Watermark বসানো হয়েছে!")
        except Exception as e:
            logger.error(f"[Watermark] process error: {e}")
            await send_msg(chat_id, f"❌ Watermark বসাতে সমস্যা হয়েছে: {e}")
        return True

    return False

async def handle_convert_command(msg: dict):
    """CSV ↔ JSON convert"""
    chat_id = msg["chat"]["id"]
    reply = msg.get("reply_to_message")
    if not reply or not reply.get("document"):
        await send_msg(chat_id, "❌ CSV বা JSON ফাইলে reply করে /convert দাও!")
        return
    try:
        file_bytes = await download_tg_file(reply["document"]["file_id"])
        file_name = reply["document"].get("file_name", "")

        if file_name.lower().endswith(".csv"):
            mcqs = _parse_csv_bytes(file_bytes)
            json_data = []
            for i, mcq in enumerate(mcqs):
                opts = mcq.get("options", [])
                json_data.append({
                    "question_number": str(i + 1),
                    "question": mcq["question"],
                    "options": {
                        "A": opts[0] if len(opts) > 0 else "",
                        "B": opts[1] if len(opts) > 1 else "",
                        "C": opts[2] if len(opts) > 2 else "",
                        "D": opts[3] if len(opts) > 3 else ""
                    },
                    "correct_answer": mcq.get("answer", "A"),
                    "explanation": mcq.get("explanation", "")
                })
            out = json.dumps(json_data, ensure_ascii=False, indent=2).encode("utf-8")
            await send_document(chat_id, out,
                file_name.replace(".csv", ".json"),
                caption=f"✅ CSV → JSON Converted! {len(json_data)} questions",
                mime_type="application/json")

        elif file_name.lower().endswith(".json"):
            json_data = json.loads(file_bytes.decode("utf-8-sig"))
            import io as _io, csv as _csv
            buf = _io.StringIO()
            writer = _csv.writer(buf)
            writer.writerow(["questions","option1","option2","option3","option4","answer","explanation","type","section"])
            for item in json_data:
                opts = item.get("options", {})
                ans_map = {"A": "1", "B": "2", "C": "3", "D": "4"}
                ans = ans_map.get(item.get("correct_answer", "A"), "1")
                writer.writerow([
                    item.get("question", ""),
                    opts.get("A", ""), opts.get("B", ""),
                    opts.get("C", ""), opts.get("D", ""),
                    ans, item.get("explanation", ""), "1", "1"
                ])
            await send_document(chat_id, buf.getvalue().encode("utf-8"),
                file_name.replace(".json", ".csv"),
                caption=f"✅ JSON → CSV Converted! {len(json_data)} questions",
                mime_type="text/csv")
        else:
            await send_msg(chat_id, "❌ Only CSV or JSON files!")
    except Exception as e:
        await send_msg(chat_id, f"❌ Error: {e}")


# ============================================================
# WEBHOOK HANDLER
# ============================================================
@app.post("/webhook")
async def webhook(request: Request):
    try:
        update = await request.json()
        asyncio.create_task(process_update(update))
        return Response("OK")
    except Exception as e:
        logger.error(f"[Webhook] Parse error: {e}")
        return Response("OK")

async def process_update(update: dict):
    try:
        if "message" in update:
            await handle_message(update["message"])
        elif "callback_query" in update:
            await handle_callback(update["callback_query"])
        elif "poll_answer" in update:
            await handle_poll_answer(update["poll_answer"])
    except Exception as e:
        logger.error(f"[Update] Error: {e}")
        await notify_owner(f"[Update] Unhandled error:\n{str(e)[:500]}")

# ============================================================
# MESSAGE HANDLER
# ============================================================
async def set_bot_commands(notify_chat_id: int = None):
    """v1.1: register Telegram's '/' command menu for both default (user) and
    admin/owner scopes. Called automatically on every bot startup (so the
    menu is always in sync after a deploy) and also via /setcommand for a
    manual refresh."""
    # ---- ADMIN/OWNER command list (full) ----
    admin_commands = [
        {"command": "start", "description": "Bot শুরু করো / সব commands দেখো"},
        {"command": "help", "description": "সব commands ও ব্যবহার দেখো"},
        {"command": "pdf", "description": "PDF থেকে MCQ generate করো"},
        {"command": "qpdf", "description": "chorcha mhtml/html → Premium Q&A PDF"},
        {"command": "pdfm", "description": "PDF pagewise MCQ with image"},
        {"command": "img", "description": "Image থেকে MCQ poll channel-এ পাঠাও"},
        {"command": "txt", "description": "Text থেকে MCQ poll"},
        {"command": "csv", "description": "CSV থেকে channel poll"},
        {"command": "csvs", "description": "CSV থেকে sequential poll (csvS)"},
        {"command": "live", "description": "CSV দিয়ে Live Quiz শুরু করো"},
        {"command": "rapid", "description": "CSV দিয়ে Scheduled Rapid Fire (comment-based) শুরু করো"},
        {"command": "livetime", "description": "Live Quiz-এর প্রতি প্রশ্নের সময় set করো"},
        {"command": "channel", "description": "Channel/Group add করো (custom name সহ)"},
        {"command": "channelist", "description": "Channel list দেখো"},
        {"command": "tagq", "description": "Poll-এ tag set করো (tagQ)"},
        {"command": "expq", "description": "Explanation footer set করো (expQ)"},
        {"command": "bm", "description": "Bookmark PDF বানাও"},
        {"command": "bmexam", "description": "Bookmark MCQ থেকে Quiz দাও"},
        {"command": "permit", "description": "Admin add করো"},
        {"command": "remove", "description": "Admin remove করো"},
        {"command": "pinon", "description": "Auto-pin চালু করো"},
        {"command": "pinoff", "description": "Auto-pin বন্ধ করো"},
        {"command": "info2", "description": "Bot stats দেখো"},
        {"command": "pdfc", "description": "Image collection শুরু করো"},
        {"command": "done", "description": "Image collection শেষ করো — PDF বানাও"},
        {"command": "q", "description": "CSV থেকে D1 quiz তৈরি করো"},
        {"command": "qlist", "description": "সব D1 quiz দেখো"},
        {"command": "qdel", "description": "D1 quiz delete করো"},
        {"command": "pre", "description": "Quiz preview image set করো"},
        {"command": "info", "description": "Quiz details দেখো"},
        {"command": "send", "description": "Quiz share করো channel-এ"},
        {"command": "collect", "description": "Poll collect mode চালু করো"},
        {"command": "merge", "description": "Collected polls merge করো"},
        {"command": "watermark", "description": "PDF-এ watermark বসাও"},
        {"command": "convert", "description": "Quiz → CSV export করো"},
        {"command": "error", "description": "সাম্প্রতিক bot error দেখো"},
        {"command": "setcommand", "description": "Bot commands register করো (Owner)"},
    ]

    # ---- USER command list (everything a regular user can actually use) ----
    user_commands = [
        {"command": "start", "description": "Bot শুরু করো"},
        {"command": "help", "description": "সাহায্য / সব commands দেখো"},
        {"command": "bm", "description": "🔖 Bookmark PDF বানাও"},
        {"command": "bmexam", "description": "🎯 Bookmark MCQ থেকে Quiz দাও"},
        {"command": "pdfc", "description": "📸 একাধিক Image → PDF বানান"},
        {"command": "done", "description": "✅ Image collection শেষ করো"},
        {"command": "cancel", "description": "❌ চলমান কাজ বাতিল করো"},
    ]

    r_default = await tg_post("setMyCommands", {
        "commands": user_commands,
        "scope": {"type": "default"}
    })

    # v1.1: explicitly set the menu button (the icon next to the chat box)
    # to show the command list. Without this, some Telegram clients don't
    # surface the '/' menu icon even if setMyCommands succeeded.
    try:
        await tg_post("setChatMenuButton", {"menu_button": {"type": "commands"}})
    except Exception as e:
        logger.error(f"[SetCommand] setChatMenuButton error: {e}")

    admin_ids = {OWNER_ID}
    try:
        admin_rows = sb.table("admins").select("user_id").execute()
        for row in (admin_rows.data or []):
            try:
                admin_ids.add(int(row["user_id"]))
            except (TypeError, ValueError):
                logger.error(f"[SetCommand] invalid admin user_id: {row.get('user_id')}")
    except Exception as e:
        logger.error(f"[SetCommand] admin fetch error: {e}")

    ok_count = 0
    for admin_id in admin_ids:
        r_admin = await tg_post("setMyCommands", {
            "commands": admin_commands,
            "scope": {"type": "chat", "chat_id": admin_id}
        })
        if r_admin.get("ok"):
            ok_count += 1
        else:
            logger.error(f"[SetCommand] failed for admin {admin_id}: {r_admin.get('description')}")

    if notify_chat_id is not None:
        if r_default.get("ok"):
            await send_msg(notify_chat_id,
                f"✅ Command list set হয়েছে!\n\n"
                f"👤 User-দের জন্য: {len(user_commands)}টি command\n"
                f"👑 {ok_count}/{len(admin_ids)} Admin-দের জন্য: {len(admin_commands)}টি command"
            )
        else:
            await send_msg(notify_chat_id, f"❌ Error: {r_default.get('description')}")
    return r_default.get("ok"), ok_count, len(admin_ids)

async def handle_message(msg: dict):
    text = msg.get("text", "").strip()
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    uname = msg["from"].get("first_name", "User")
    chat_type = msg["chat"].get("type", "private")
    is_private = chat_type == "private"
    await db_track_user(uid, uname)
    is_auth = await db_is_owner_or_admin(uid)

    # Image collection mode check
    if msg.get("photo") or msg.get("document"):
        collected = await handle_incoming_image_for_collection(msg)
        if collected:
            return

    # v1.2: Watermark flow check (awaiting PDF, then awaiting text)
    if uid in WATERMARK_PENDING:
        consumed = await handle_watermark_document(msg)
        if consumed:
            return

    # v1.3: /rapid flow check (awaiting local time text after channel select)
    if uid in RAPID_PENDING and msg.get("text") and not text.startswith("/"):
        consumed = await handle_rapid_time_text(msg)
        if consumed:
            return

    # DB cleanup (every ~100 requests, random)
    if random.random() < 0.01:
        asyncio.create_task(db_auto_cleanup_if_needed())

    # Poll auto-collect (forwarded polls)
    if msg.get("poll") and msg.get("forward_date"):
        collected = await handle_poll_auto_collect(msg)
        if collected:
            return

    if text == "/start":
        await handle_start(msg)
        return
    if text == "/help":
        await handle_start(msg)
        return
    if text.startswith("/start premium_"):
        cache_id = text.replace("/start premium_", "").strip()
        asyncio.create_task(handle_premium_pdf_start(msg, cache_id))
        return
    if text.startswith("/start pdf_"):
        cache_id = text.replace("/start pdf_", "").strip()
        asyncio.create_task(handle_quiz_solve(msg, cache_id))
        return
    if text.startswith("/start poll_"):
        cache_id = text.replace("/start poll_", "").strip()
        asyncio.create_task(handle_poll_again(cache_id, msg["from"], chat_id))
        return
    if text.startswith("/start qz_"):
        quiz_id = text.split()[1] if len(text.split()) > 1 else text.replace("/start ", "")
        asyncio.create_task(start_d1_quiz(chat_id, quiz_id, msg["from"]))
        return
    if text.startswith("/pdf") and not text.startswith("/pdfc") and not text.startswith("/pdfm"):
        if not is_auth:
            if is_private:
                await send_msg(chat_id, UNAUTH_MSG)
            return
        await handle_pdf(msg)
        return
    if text == "/bm":
        await handle_bm(msg)
        return
    if text == "/bmexam":
        asyncio.create_task(handle_bmexam(msg))
        return
    if text in ("/collect", "/cstatus", "/cdone", "/ccancel"):
        await handle_collect_command(msg)
        return
    if not is_auth:
        if is_private:
            await send_msg(chat_id, UNAUTH_MSG)
        return
    if text.startswith("/permit"):
        await handle_permit(msg)
    elif text.startswith("/remove"):
        await handle_remove(msg)
    elif text.startswith("/tagQ"):
        await handle_tagQ(msg)
    elif text.startswith("/expQ"):
        await handle_expQ(msg)
    elif text.startswith("/channel") or text == "/channelist":
        await handle_channel(msg)
    elif text == "/info2":
        await handle_info2(msg)
    elif text.startswith("/pdfm"):
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        await handle_pdfm(msg)
    elif text.startswith("/img"):
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        await handle_img_command(msg)
    elif text.startswith("/txt"):
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        await handle_txt_command(msg)
    elif text.startswith("/qpdf"):
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        asyncio.create_task(handle_qpdf_command(msg))
    elif text.startswith("/csvS"):
        # /csvS অবশ্যই /csv এর আগে check করতে হবে
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        asyncio.create_task(handle_csvs_command(msg))
    elif text.startswith("/csv"):
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        asyncio.create_task(handle_csv_command(msg))
    elif text.startswith("/rapid ") or text == "/rapid":
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        asyncio.create_task(handle_rapid_command(msg))
    elif text.startswith("/wm"):
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        asyncio.create_task(handle_wm_command(msg))
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        asyncio.create_task(handle_live_command(msg))

    elif text.startswith("/pollcsv") or text.startswith("/pcsv"):
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        asyncio.create_task(handle_poll_extract(msg))

    elif text.startswith("/poll") and "\n" in text and "t.me/" in text:
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        asyncio.create_task(handle_poll_extract(msg))

    elif text == "/setcommand":
        if uid != OWNER_ID:
            await send_msg(chat_id, "❌ Owner only!")
            return
        await set_bot_commands(notify_chat_id=chat_id)
    elif text.startswith("/livetime"):
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        await handle_livetime(msg)
    elif text == "/pin" or text.startswith("/pin "):
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        await handle_pin(msg)
    elif text in ("/pdfc", "/pdf_collect", "/done", "/cancel"):
        await handle_pdf_image_mode(msg)
    elif text.startswith("/q") and not text.startswith("/qlist") and not text.startswith("/qdel"):
        await handle_quiz_create(msg)
    elif text == "/qlist":
        await handle_qlist(msg)
    elif text.startswith("/qdel"):
        await handle_qdel(msg)
    elif text.startswith("/pre"):
        await handle_d1_pre(msg)
    elif text == "/info":
        if uid == OWNER_ID:
            await handle_d1_info(msg)
        else:
            await send_msg(chat_id, "❌ Owner only!")
    elif text == "/send":
        if uid == OWNER_ID:
            await handle_d1_send(msg)
        else:
            await send_msg(chat_id, "❌ Owner only!")
    elif text.startswith("/merge"):
        await handle_merge_command(msg)
    elif text == "/watermark":
        await handle_watermark_command(msg)
    elif text == "/convert":
        await handle_convert_command(msg)
    elif text.startswith("/error") or text.startswith("/errors"):
        await handle_error_command(msg)
    elif text == "/ping":
        try:
            uptime_seconds = int(time.time() - BOT_START_TIME)
            days, rem = divmod(uptime_seconds, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, _ = divmod(rem, 60)
            uptime_str = (f"{days}d " if days else "") + f"{hours}h {minutes}m"

            bd_tz = pytz.timezone("Asia/Dhaka")
            started_at = datetime.fromtimestamp(BOT_START_TIME, bd_tz).strftime("%d-%b %I:%M %p")

            today_start = datetime.now(bd_tz).replace(hour=0, minute=0, second=0, microsecond=0)
            today_start_ts = int(today_start.timestamp())

            total_users = 0
            daily_active = 0
            try:
                total_r = sb.table("pdf_users").select("user_id", count="exact").execute()
                total_users = total_r.count or 0
                active_r = sb.table("pdf_users").select("user_id", count="exact") \
                    .gte("last_seen", today_start_ts).execute()
                daily_active = active_r.count or 0
            except Exception as e:
                logger.error(f"[Ping] user count error: {e}")

            key_count = len(key_rotator.keys)

            await send_msg(chat_id,
                "🏓 <b>Pong! ATLAS Bot Online</b>\n\n"
                f"🕐 চালু হয়েছে: {started_at}\n"
                f"⏱ Active আছে: {uptime_str}\n"
                f"🔑 Gemini Keys: {key_count}\n"
                f"👥 Total Users: {total_users}\n"
                f"🟢 আজকে Active: {daily_active}"
            )
        except Exception as e:
            logger.error(f"[Ping] error: {e}")
            await send_msg(chat_id, f"🏓 Pong! (stats error: {e})")

# ============================================================
# CALLBACK HANDLER
# ============================================================
async def handle_callback(query: dict):
    data = query["data"]
    chat_id = query["message"]["chat"]["id"]
    uid = query["from"]["id"]
    msg_id = query["message"]["message_id"]
    user = query["from"]
    uname = user.get("username") or user.get("first_name", "User")
    await tg_post("answerCallbackQuery", {"callback_query_id": query["id"]})
    try:
        if data.startswith("pdfch_"):
            parts = data.split("_")
            channel = parts[1]
            orig_uid = int(parts[2])
            if uid != orig_uid:
                return
            row = sb.table("quiz_sessions").select("data").eq("key", f"pdf_pending_{uid}").execute()
            if not row.data:
                await send_msg(chat_id, "❌ Session expired!")
                return
            pending = json.loads(row.data[0]["data"])
            saved_thread_id = pending.get("thread_id")
            pages = getattr(app.state, "pdf_cache", {}).get(f"pdf_img_{uid}")
            if not pages:
                saved_file_id = pending.get("file_id")
                if not saved_file_id:
                    await send_msg(chat_id, "❌ Session expired!")
                    return
                await send_msg(chat_id, "⏳ PDF re-download হচ্ছে...")
                try:
                    pdf_bytes = await download_tg_file(saved_file_id)
                    pages = await asyncio.to_thread(pdf_to_images, pdf_bytes, pending.get("page_range"))
                except Exception as e:
                    await send_msg(chat_id, f"❌ PDF re-download failed: {e}")
                    return
                if not pages:
                    await send_msg(chat_id, "❌ Page পাওয়া যায়নি!")
                    return
            if channel == "csv":
                await process_pdf_pages(chat_id, uid, user.get("first_name", "User"), pages,
                    pending["topic"], pending.get("mcq_count"), None, True,
                    pending.get("file_name", "document.pdf"), pending.get("status_msg_id"),
                    thread_id=saved_thread_id)
            else:
                await process_pdf_pages(chat_id, uid, user.get("first_name", "User"), pages,
                    pending["topic"], pending.get("mcq_count"), channel, False,
                    pending.get("file_name", "document.pdf"), pending.get("status_msg_id"),
                    thread_id=saved_thread_id)
            getattr(app.state, "pdf_cache", {}).pop(f"pdf_img_{uid}", None)

        elif data.startswith("pollagain_"):
            cache_id = data.replace("pollagain_", "")
            asyncio.create_task(handle_poll_again(cache_id, user, chat_id))

        elif data.startswith("pollnew_"):
            cache_id = data.replace("pollnew_", "")
            asyncio.create_task(handle_poll_new(cache_id, user, chat_id, msg_id))

        elif data.startswith("polllb_"):
            cache_id = data.replace("polllb_", "")
            await handle_poll_leaderboard(cache_id, uid, chat_id)

        elif data.startswith("qnew_"):
            cache_id = data.replace("qnew_", "")
            asyncio.create_task(handle_quiz_new(cache_id, user, chat_id))

        elif data == "qmis":
            asyncio.create_task(handle_quiz_practice(uid, chat_id, uname, "mis"))

        elif data == "qspe":
            asyncio.create_task(handle_quiz_practice(uid, chat_id, uname, "spe"))

        elif data == "bm_pdf":
            fake_msg = {"chat": {"id": chat_id}, "from": {"id": uid, "first_name": uname}}
            await handle_bm(fake_msg)

        elif data == "bmexam_again":
            fake_msg = {"chat": {"id": chat_id}, "from": {"id": uid, "first_name": uname}}
            asyncio.create_task(handle_bmexam(fake_msg))

        elif data.startswith("imgmode_"):
            parts = data.split("_")
            mode = parts[1]  # "image" or "topic"
            orig_uid = int(parts[2])
            if uid != orig_uid:
                return
            await handle_img_mode(mode, uid, chat_id, user)

        elif data.startswith("imgchannel_"):
            parts = data.split("_", 2)
            channel = parts[1]
            orig_uid = int(parts[2])
            if uid != orig_uid:
                return
            row = sb.table("quiz_sessions").select("data").eq("key", f"img_mode_{uid}").execute()
            if not row.data:
                await send_msg(chat_id, "❌ Session expired!")
                return
            img_data = json.loads(row.data[0]["data"])
            asyncio.create_task(process_img_to_poll(
                img_data["file_id"], channel, img_data["mode"],
                chat_id, uid, uname, topic=img_data.get("topic", "ATLAS Special MCQ")
            ))

        elif data.startswith("txtchannel_"):
            parts = data.split("_", 2)
            channel = parts[1]
            orig_uid = int(parts[2])
            if uid != orig_uid:
                return
            row = sb.table("quiz_sessions").select("data").eq("key", f"txt_cmd_{uid}").execute()
            if not row.data:
                await send_msg(chat_id, "❌ Session expired!")
                return
            txt_data = json.loads(row.data[0]["data"])
            asyncio.create_task(process_txt_to_poll(
                txt_data["text"], channel, chat_id, uid, uname
            ))

        elif data.startswith("csvact_"):
            # csvact_{action}_{cache_id}_{uid}
            parts = data.split("_", 3)
            action = parts[1]
            rest = parts[2] if len(parts) > 2 else ""
            # cache_id may contain _ so split from right
            rest_parts = rest.rsplit("_", 1)
            cache_id_cb = rest_parts[0] if len(rest_parts) > 1 else rest
            orig_uid = int(rest_parts[1]) if len(rest_parts) > 1 else uid
            if uid != orig_uid:
                return
            row = sb.table("quiz_sessions").select("data").eq("key", f"csv_cmd_{uid}").execute()
            if not row.data:
                await send_msg(chat_id, "❌ Session expired! আবার CSV reply করে /csv দাও।")
                return
            csv_data = json.loads(row.data[0]["data"])
            c_id = csv_data["cache_id"]
            topic_cb = csv_data.get("topic", "MCQ")

            if action == "quiz":
                # D1 quiz হিসেবে save করে bot link দাও
                mcqs_row = await db_get_mcq_cache(c_id)
                if mcqs_row:
                    from quiz import create_quiz_from_mcqs
                    quiz_id = await create_quiz_from_mcqs(mcqs_row["mcq_data"], topic_cb, uid)
                    bot_info = await tg_post("getMe", {})
                    bot_un = bot_info.get("result", {}).get("username", "")
                    await send_msg(chat_id,
                        f"🎯 <b>Quiz তৈরি হয়েছে!</b>\n\n"
                        f"🔗 <code>https://t.me/{bot_un}?start={quiz_id}</code>",
                        parse_mode="HTML"
                    )

            elif action == "poll":
                # Channel select করতে বলো — poll পাঠাবে
                channels = await db_get_channels()
                if not channels:
                    await send_msg(chat_id, "❌ Channel নেই! /channel দিয়ে add করো।")
                    return
                kb2 = {"inline_keyboard": []}
                for ch in channels:
                    kb2["inline_keyboard"].append([{
                        "text": f"📢 {ch.get('channel_name', ch.get('channel_id'))}",
                        "callback_data": f"csvchannel_{ch['channel_id']}_{uid}"
                    }])
                kb2["inline_keyboard"].append([{"text": "❌ Cancel", "callback_data": f"csvcancel_{uid}"}])
                await send_msg(chat_id, "📢 Channel select করো:", reply_markup=kb2)

            elif action == "web":
                # D1 তে save করে web link দাও
                mcqs_row = await db_get_mcq_cache(c_id)
                if mcqs_row:
                    from poll_extract import save_quiz_to_d1
                    polls = [{"question": q["question"], "options": q["options"],
                               "correct_idx": ["A","B","C","D","E"].index(q.get("answer","A")) if q.get("answer","A") in ["A","B","C","D","E"] else 0,
                               "explanation": q.get("explanation","")}
                              for q in mcqs_row["mcq_data"]]
                    quiz_id = await save_quiz_to_d1(polls, topic_cb, uid)
                    web_url = f"https://atlasquizbotpro.hamza818483.workers.dev/quiz/{quiz_id}"
                    await send_msg(chat_id,
                        f"🌐 <b>Web Exam Link:</b>\n{web_url}",
                        parse_mode="HTML"
                    )

            elif action == "pdf":
                # existing pdfm flow use করো
                mcqs_row = await db_get_mcq_cache(c_id)
                if not mcqs_row:
                    await send_msg(chat_id, "❌ Session expired!")
                    return
                uname = msg.get("from", {}).get("username", "user")
                pages = [mcqs_row["mcq_data"]]
                asyncio.create_task(process_pdfm_pages(
                    chat_id, uid, uname, pages, topic_cb,
                    None, None, None, None
                ))

            elif action == "channel":
                channels = await db_get_channels()
                if not channels:
                    await send_msg(chat_id, "❌ Channel নেই!")
                    return
                kb2 = {"inline_keyboard": []}
                for ch in channels:
                    kb2["inline_keyboard"].append([{
                        "text": f"📢 {ch.get('channel_name', ch.get('channel_id'))}",
                        "callback_data": f"csvchannel_{ch['channel_id']}_{uid}"
                    }])
                kb2["inline_keyboard"].append([{"text": "❌ Cancel", "callback_data": f"csvcancel_{uid}"}])
                await send_msg(chat_id, "📢 Channel select করো:", reply_markup=kb2)

        elif data.startswith("csvchannel_"):
            parts = data.split("_", 2)
            channel = parts[1]
            orig_uid = int(parts[2])
            if uid != orig_uid:
                return
            row = sb.table("quiz_sessions").select("data").eq("key", f"csv_cmd_{uid}").execute()
            if not row.data:
                await send_msg(chat_id, "❌ Session expired!")
                return
            csv_data = json.loads(row.data[0]["data"])
            asyncio.create_task(process_csv_to_channel(
                csv_data["cache_id"], channel, chat_id, uid
            ))

        elif data.startswith("rapidch_"):
            parts = data.split("_", 2)
            channel = parts[1]
            orig_uid = int(parts[2])
            if uid != orig_uid:
                return
            state = RAPID_PENDING.get(uid)
            if not state or state.get("step") != "awaiting_channel":
                await send_msg(chat_id, "❌ Session expired! আবার /rapid দাও।")
                return
            state["channel_id"] = channel
            state["step"] = "awaiting_time"
            await send_msg(chat_id,
                "🕐 কখন শুরু হবে? Local time (Asia/Dhaka) লিখো:\n\n"
                "<b>Example:</b> <code>9:00 AM</code> অথবা <code>10:02 PM</code>"
            )

        elif data.startswith("rapidcancel_"):
            orig_uid = int(data.replace("rapidcancel_", ""))
            if uid != orig_uid:
                return
            RAPID_PENDING.pop(uid, None)
            await tg_post("editMessageText", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": "❌ Cancelled!"
            })

        elif data.startswith("csvcancel_"):
            orig_uid = int(data.replace("csvcancel_", ""))
            if uid != orig_uid:
                return
            await tg_post("editMessageText", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": "❌ Cancelled!"
            })

        elif data.startswith("pdfmch_"):
            parts = data.split("_")
            channel = parts[1]
            orig_uid = int(parts[2])
            if uid != orig_uid:
                return
            row = sb.table("quiz_sessions").select("data").eq("key", f"pdfm_pending_{uid}").execute()
            if not row.data:
                await send_msg(chat_id, "❌ Session expired!")
                return
            pending = json.loads(row.data[0]["data"])
            pages = getattr(app.state,"pdf_cache",{}).get(f"pdfm_img_{uid}")
            if not pages:
                saved_file_id = pending.get("file_id")
                if not saved_file_id:
                    await send_msg(chat_id, "❌ Session expired!")
                    return
                await send_msg(chat_id, "⏳ PDF re-download হচ্ছে...")
                try:
                    pdf_bytes = await download_tg_file(saved_file_id)
                    pages = await asyncio.to_thread(pdf_to_images, pdf_bytes, pending.get("page_range"))
                except Exception as e:
                    await send_msg(chat_id, f"❌ PDF re-download failed: {e}")
                    return
                if not pages:
                    await send_msg(chat_id, "❌ Page পাওয়া যায়নি!")
                    return
            csv_only = channel == "csv"
            ch = None if csv_only else channel
            asyncio.create_task(process_pdfm_pages(
                chat_id, uid, user.get("first_name","User"), pages,
                pending["topic"], pending.get("mcq_count"), ch, csv_only,
                pending.get("file_name","document.pdf"),
                pending.get("status_msg_id"),
                thread_id=pending.get("thread_id")
            ))
            getattr(app.state,"pdf_cache",{}).pop(f"pdfm_img_{uid}", None)

        elif data.startswith("livechannel_"):
            parts    = data.split("_", 2)
            channel  = parts[1]
            orig_uid = int(parts[2])
            if uid != orig_uid:
                return

            row = sb.table("quiz_sessions").select("data")\
                .eq("key", f"live_pending_{uid}").execute()
            if not row.data:
                await send_msg(chat_id, "❌ Session expired!")
                return

            live_data = json.loads(row.data[0]["data"])

            if channel in LIVE_QUIZ_STATE:
                await send_msg(chat_id,
                    "❌ এই group-এ আগে থেকেই Live Quiz চলছে!")
                return

            per_q_time = await db_get_live_time(chat_id)

            await send_msg(chat_id,
                f"✅ Live Quiz শুরু হচ্ছে!\n"
                f"📢 Group: {channel}\n"
                f"⚡ {per_q_time} sec/question\n"
                f"📝 {len(live_data['mcqs'])} MCQ"
            )

            asyncio.create_task(start_live_quiz(
                channel,
                live_data["session_id"],
                live_data["topic"],
                live_data["mcqs"],
                live_data.get("admin_chat", chat_id),
                per_q_time
            ))

        elif data.startswith("livecancel_"):
            orig_uid = int(data.replace("livecancel_", ""))
            if uid != orig_uid:
                return
            await tg_post("editMessageText", {
                "chat_id":    chat_id,
                "message_id": msg_id,
                "text":       "❌ Live Quiz cancelled!"
            })

        # Bookmark Exam — count select
        elif data.startswith("bmex_"):
            parts = data.split("_")
            count_choice = parts[1]
            target_uid = int(parts[2])
            if uid == target_uid:
                asyncio.create_task(handle_bmexam_start(chat_id, uid, uname, count_choice))

        # D1 Quiz System callbacks
        elif data.startswith("qznext_"):
            target_uid = int(data.replace("qznext_", ""))
            if uid == target_uid:
                asyncio.create_task(handle_quiz_next(uid))

        elif data.startswith("qzlb_"):
            quiz_id = data.replace("qzlb_", "")
            await handle_d1_leaderboard(chat_id, quiz_id, uid)

        elif data.startswith("qzhist_"):
            quiz_id = data.replace("qzhist_", "")
            await handle_d1_history(chat_id, quiz_id, uid)

        elif data.startswith("qzmp1_"):
            quiz_id = data.replace("qzmp1_", "")
            asyncio.create_task(handle_d1_mistake(chat_id, quiz_id, uid, user, "wrong"))

        elif data.startswith("qzmp2_"):
            quiz_id = data.replace("qzmp2_", "")
            asyncio.create_task(handle_d1_mistake(chat_id, quiz_id, uid, user, "wrong+skip"))

        elif data.startswith("d1send_"):
            await handle_d1_send_cb(query)

    except Exception as e:
        logger.error(f"[CB] Error: {e}")
        await send_msg(chat_id, f"❌ Error: {e}")

# ============================================================
# POLL LEADERBOARD
# ============================================================
async def handle_poll_leaderboard(cache_id: str, uid: int, chat_id: int):
    try:
        cache = await db_get_mcq_cache(cache_id)
        if cache and cache.get("is_new_gen"):
            await send_msg(chat_id, "❌ New Quiz/Exam এ leaderboard নেই!")
            return
        r = sb.table("web_exam_leaderboard").select("*")\
            .eq("cache_id", cache_id).order("final_score", desc=True).limit(50).execute()
        lb = r.data or []
        if not lb:
            await send_msg(chat_id, "🏆 এখনো কেউ exam দেয়নি!")
            return
        medals = ["🥇", "🥈", "🥉"]
        txt = f"🏆 Leaderboard\n{lb[0].get('topic', '')} — Page No: {fmt_page(lb[0].get('page_number',1))}\n\n"
        for i, row in enumerate(lb):
            is_me = row["user_id"] == uid
            medal = medals[i] if i < 3 else f"{i+1}."
            pct = round(row["correct"] / row["total"] * 100) if row["total"] else 0
            txt += f"{medal} {row['user_name']} — {row['final_score']}/{row['total']} ({pct}%)"
            if is_me:
                txt += " 👈 You"
            txt += "\n"
        await send_msg(chat_id, txt)
    except Exception as e:
        logger.error(f"[LB] Error: {e}")

# ============================================================
# EXAM API ROUTES
# ============================================================
@app.get("/exam/{cache_id}", response_class=HTMLResponse)
async def exam_page(cache_id: str, request: Request):
    try:
        uid = request.query_params.get("uid", "")
        name = request.query_params.get("name", "Student")
        with open("/app/index.html", "r", encoding="utf-8") as f:
            html = f.read()
        html = html.replace("{{CACHE_ID}}", cache_id)
        html = html.replace("{{USER_ID}}", uid)
        html = html.replace("{{USER_NAME}}", name)
        html = html.replace("{{SUPABASE_URL}}", SUPABASE_URL)
        html = html.replace("{{SUPABASE_KEY}}", SUPABASE_KEY)
        html = html.replace("{{HF_SPACE_URL}}", HF_SPACE_URL)
        return HTMLResponse(html)
    except FileNotFoundError:
        return HTMLResponse("<h1>Exam page not found</h1>", status_code=404)

@app.get("/api/exam/{cache_id}")
async def get_exam_data(cache_id: str):
    try:
        # qz_ prefix মানে D1 quiz — poll_extract থেকে আসা
        if cache_id.startswith("qz_"):
            rows = await d1_select("SELECT * FROM quizzes WHERE id=?1", [cache_id])
            if not rows:
                # Layer 2: Supabase backup থেকে restore
                try:
                    import httpx as _hx
                    _h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
                    async with _hx.AsyncClient(timeout=10) as _c:
                        _r = await _c.get(f"{SUPABASE_URL}/rest/v1/quiz_backups",
                            headers=_h, params={"quiz_id": f"eq.{cache_id}", "select": "*"})
                    _b = _r.json()
                    if _b:
                        _bk = _b[0]
                        await d1_run(
                            "INSERT OR REPLACE INTO quizzes (id,name,description,timer,shuffle,csv_data,tag,exp_footer,created_by) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                            [cache_id, _bk["name"], "", 30, 0, json.dumps(_bk["questions"]), "", "", 0]
                        )
                        rows = await d1_select("SELECT * FROM quizzes WHERE id=?1", [cache_id])
                except Exception as _e:
                    logger.warning(f"[exam] Supabase restore failed: {_e}")

            if not rows:
                # Layer 3: CF Worker fallback
                try:
                    import httpx as _hx
                    CF_QUIZ_URL = f"https://atlasquizbotpro.hamza818483.workers.dev/api/exam/{cache_id}"
                    async with _hx.AsyncClient(timeout=8) as _c:
                        _r = await _c.get(CF_QUIZ_URL)
                    if _r.status_code == 200:
                        return JSONResponse(_r.json())
                except Exception as _e:
                    logger.warning(f"[exam] CF fallback failed: {_e}")

            if not rows:
                return JSONResponse({"error": "Quiz not found"}, status_code=404)
            row = rows[0]
            questions = json.loads(row.get("csv_data", "[]"))
            # index.html এর mcqs format এ convert
            mcqs = []
            for q in questions:
                opts = q.get("options", [])
                ans_idx = q.get("answer_index", 0)
                ans_labels = ["A","B","C","D","E"]
                mcqs.append({
                    "question": q.get("question",""),
                    "options": opts,
                    "answer": ans_labels[ans_idx] if ans_idx < len(ans_labels) else "A",
                    "explanation": q.get("explanation",""),
                })
            return JSONResponse({
                "cache_id": cache_id,
                "topic": row.get("name", "Quiz"),
                "page": 1,
                "mcqs": mcqs,
                "tag": row.get("tag",""),
                "exp_footer": row.get("exp_footer",""),
                "channel_id": "",
                "image_msg_id": None,
                "end_msg_id": None,
                "image_file_id": None,
                "is_new_gen": False,
                "timer": row.get("timer", 30),
            })

        # Normal cache_id — existing system
        cache = await db_get_mcq_cache(cache_id)
        if not cache:
            return JSONResponse({"error": "Not found"}, status_code=404)
        settings = await db_get_settings()
        return JSONResponse({
            "cache_id": cache_id, "topic": cache["topic"], "page": cache["page_number"],
            "mcqs": cache["mcq_data"], "tag": settings.get("tag", ""),
            "exp_footer": settings.get("exp_footer", ""), "channel_id": cache.get("channel_id", ""),
            "image_msg_id": cache.get("image_msg_id"), "end_msg_id": cache.get("end_msg_id"),
            "image_file_id": cache.get("image_file_id"), "is_new_gen": bool(cache.get("is_new_gen"))
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/tg-image/{file_id}")
async def tg_image_proxy(file_id: str):
    try:
        content = await download_tg_file(file_id)
        return Response(content=content, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=404)

@app.post("/api/solve-pdf")
async def solve_pdf(request: Request):
    try:
        data = await request.json()
        cache_id = data.get("cache_id")
        answers = data.get("answers") or {}
        cache = await db_get_mcq_cache(cache_id)
        if not cache:
            return JSONResponse({"error": "Cache not found"}, status_code=404)
        html = _build_solve_sheet_html(cache["topic"], cache["page_number"], cache["mcq_data"], answers)
        pdf_bytes = await _html_to_pdf(html)
        if not pdf_bytes:
            return JSONResponse({"error": "PDF generation failed"}, status_code=500)
        return JSONResponse({"ok": True, "pdf_b64": base64.b64encode(pdf_bytes).decode()})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/exam/result")
async def save_exam_result(request: Request):
    try:
        data = await request.json()
        cache_id = data.get("cache_id")
        user_id = data.get("user_id")
        user_name = data.get("user_name", "User")
        topic = data.get("topic", "")
        page = data.get("page", 0)
        total = data.get("total", 0)
        correct = data.get("correct", 0)
        wrong = data.get("wrong", 0)
        skipped = data.get("skipped", 0)
        time_taken = data.get("time_taken", 0)
        negative = round(wrong * 0.25, 2)
        final_score = round(correct - negative, 2)
        sb.table("web_exam_results").insert({
            "cache_id": cache_id, "user_id": user_id, "user_name": user_name,
            "topic": topic, "page_number": page, "total": total,
            "correct": correct, "wrong": wrong, "skipped": skipped,
            "negative_marks": negative, "final_score": final_score, "time_taken": time_taken
        }).execute()
        cache = await db_get_mcq_cache(cache_id)
        if not (cache and cache.get("is_new_gen")):
            await db_save_leaderboard(cache_id, user_id, user_name, topic, page, correct, total, final_score)
        pct = round(correct / total * 100) if total else 0
        return JSONResponse({
            "ok": True, "final_score": final_score, "negative": negative,
            "pct": pct, "motivation": get_motivation(pct), "ayat": get_random_ayat()
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/leaderboard/{cache_id}")
async def get_leaderboard(cache_id: str):
    try:
        cache = await db_get_mcq_cache(cache_id)
        if cache and cache.get("is_new_gen"):
            return JSONResponse({"ok": True, "disabled": True, "data": []})
        r = sb.table("web_exam_leaderboard").select("*")\
            .eq("cache_id", cache_id).order("final_score", desc=True).limit(50).execute()
        return JSONResponse({"ok": True, "data": r.data or []})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/bookmark")
async def save_bookmark(request: Request):
    try:
        data = await request.json()
        sb.table("bookmarks").upsert({
            "user_id": data["user_id"], "cache_id": data.get("cache_id"),
            "question_index": data.get("question_index"),
            "question_data": data.get("question_data"),
            "topic": data.get("topic"), "page_number": data.get("page")
        }, on_conflict="user_id,cache_id,question_index").execute()
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"[Bookmark] save error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.delete("/api/bookmark")
async def delete_bookmark(request: Request):
    try:
        data = await request.json()
        sb.table("bookmarks").delete()\
            .eq("user_id", data["user_id"])\
            .eq("cache_id", data["cache_id"])\
            .eq("question_index", data["question_index"]).execute()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/new-exam/start")
async def generate_new_exam_start(request: Request):
    """
    v1.3: এখন এই endpoint শুধু job শুরু করে আর সাথে সাথে job_id রিটার্ন করে —
    আসল MCQ generation ব্যাকগ্রাউন্ডে চলে, যাতে frontend instant progress page
    দেখাতে পারে (page no., ETA, % progress)।
    """
    try:
        data = await request.json()
        cache_id = data.get("cache_id")
        user_id = data.get("user_id")
        count = await db_get_new_gen_count(cache_id, user_id)
        if count >= 5:
            return JSONResponse({"error": "limit_reached", "message": "Maximum 5 বার!"}, status_code=400)
        cache = await db_get_mcq_cache(cache_id)
        if not cache:
            return JSONResponse({"error": "Cache not found"}, status_code=404)
        image_file_id = cache.get("image_file_id")
        if not image_file_id:
            return JSONResponse({"error": "Image not found"}, status_code=404)

        job_id = gen_session_id()
        NEW_EXAM_JOBS[job_id] = {
            "status": "running",
            "pct": 0,
            "page": cache.get("page_number", 0),
            "eta_sec": 18,           # initial estimate, refined as it progresses
            "started_at": time.time(),
            "new_cache_id": None,
            "error": None,
        }
        asyncio.create_task(_run_new_exam_job(job_id, cache_id, user_id, cache, image_file_id))
        return JSONResponse({"ok": True, "job_id": job_id, "page": cache.get("page_number", 0)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def _run_new_exam_job(job_id: str, cache_id: str, user_id, cache: dict, image_file_id: str):
    """Background runner — NEW_EXAM_JOBS[job_id] আপডেট করতে থাকে যতক্ষণ MCQ generation চলে।"""
    job = NEW_EXAM_JOBS[job_id]
    AVG_GEN_SECONDS = 16  # rolling estimate for ETA countdown (typical Gemini vision call time)
    try:
        job["pct"] = 8
        img_bytes = await download_tg_file(image_file_id)
        from PIL import Image as PILImage
        img = PILImage.open(BytesIO(img_bytes))
        job["pct"] = 18

        # Fake-but-honest incremental progress while the single AI call runs:
        # we don't get true sub-progress from generate_new_mcq, so tick the
        # bar upward on a timer alongside the real call, capped at 90% until
        # the call actually returns.
        async def _ticker():
            while job["status"] == "running" and job["pct"] < 90:
                await asyncio.sleep(1)
                elapsed = time.time() - job["started_at"]
                job["eta_sec"] = max(0, round(AVG_GEN_SECONDS - elapsed))
                if job["pct"] < 90:
                    job["pct"] = min(90, job["pct"] + 4)

        ticker_task = asyncio.create_task(_ticker())
        new_mcqs = await generate_new_mcq(img, cache["topic"], cache["page_number"], count=15)
        ticker_task.cancel()

        if not new_mcqs:
            job["status"] = "error"
            job["error"] = "MCQ generation failed"
            return

        job["pct"] = 95
        new_cache_id = gen_session_id()
        await db_save_mcq_cache(new_cache_id, new_cache_id, cache["page_number"], cache["topic"],
            new_mcqs, [], image_file_id, cache.get("image_msg_id"),
            cache.get("channel_id"), is_new_gen=True, end_msg_id=cache.get("end_msg_id"))
        await db_increment_gen_count(cache_id, user_id)

        job["new_cache_id"] = new_cache_id
        job["pct"] = 100
        job["eta_sec"] = 0
        job["status"] = "done"
    except Exception as e:
        logger.error(f"[NewExamJob] {job_id} error: {e}")
        job["status"] = "error"
        job["error"] = str(e)


@app.get("/api/new-exam/status/{job_id}")
async def get_new_exam_status(job_id: str):
    job = NEW_EXAM_JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "job_not_found"}, status_code=404)
    resp = {
        "ok": True,
        "status": job["status"],
        "pct": job["pct"],
        "page": job["page"],
        "eta_sec": job["eta_sec"],
    }
    if job["status"] == "done":
        cache = await db_get_mcq_cache(job["new_cache_id"])
        settings = await db_get_settings()
        resp.update({
            "new_cache_id": job["new_cache_id"],
            "mcqs": cache["mcq_data"] if cache else [],
            "tag": settings.get("tag", ""),
            "exp_footer": settings.get("exp_footer", ""),
        })
        # job state can be discarded once delivered
        NEW_EXAM_JOBS.pop(job_id, None)
    elif job["status"] == "error":
        resp["error"] = job.get("error", "unknown_error")
        NEW_EXAM_JOBS.pop(job_id, None)
    return JSONResponse(resp)


# Backward-compatible alias (old frontend builds may still call this synchronously)
@app.post("/api/new-exam")
async def generate_new_exam(request: Request):
    try:
        data = await request.json()
        cache_id = data.get("cache_id")
        user_id = data.get("user_id")
        count = await db_get_new_gen_count(cache_id, user_id)
        if count >= 5:
            return JSONResponse({"error": "limit_reached", "message": "Maximum 5 বার!"}, status_code=400)
        cache = await db_get_mcq_cache(cache_id)
        if not cache:
            return JSONResponse({"error": "Cache not found"}, status_code=404)
        image_file_id = cache.get("image_file_id")
        if not image_file_id:
            return JSONResponse({"error": "Image not found"}, status_code=404)
        img_bytes = await download_tg_file(image_file_id)
        from PIL import Image as PILImage
        img = PILImage.open(BytesIO(img_bytes))
        new_mcqs = await generate_new_mcq(img, cache["topic"], cache["page_number"], count=15)
        if not new_mcqs:
            return JSONResponse({"error": "MCQ generation failed"}, status_code=500)
        new_cache_id = gen_session_id()
        await db_save_mcq_cache(new_cache_id, new_cache_id, cache["page_number"], cache["topic"],
            new_mcqs, [], image_file_id, cache.get("image_msg_id"),
            cache.get("channel_id"), is_new_gen=True, end_msg_id=cache.get("end_msg_id"))
        await db_increment_gen_count(cache_id, user_id)
        settings = await db_get_settings()
        return JSONResponse({
            "ok": True, "new_cache_id": new_cache_id, "mcqs": new_mcqs,
            "tag": settings.get("tag", ""), "exp_footer": settings.get("exp_footer", ""),
            "remaining": 5 - (count + 1)
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ============================================================
# HEALTH CHECK
# ============================================================
@app.get("/")
async def root():
    return {"status": "ok", "bot": "ATLAS BOT", "version": "4.2.0"}

@app.get("/health")
async def health():
    return {"status": "ok", "db": sb is not None, "gemini_keys": len(key_rotator.keys), "bot_token": bool(BOT_TOKEN)}

@app.on_event("startup")
async def startup():
    global BOT_START_TIME
    BOT_START_TIME = time.time()
    logger.info("[App] ATLAS BOT v4.1 starting...")
    if not BOT_TOKEN:
        logger.error("[App] BOT_TOKEN missing!")
        return
    logger.info("[App] Using CF Worker proxy for TG API")
    try:
        ok, admin_ok, admin_total = await set_bot_commands()
        logger.info(f"[App] Command menu set on startup: default={ok}, admins={admin_ok}/{admin_total}")
    except Exception as e:
        logger.error(f"[App] Failed to set command menu on startup: {e}")
    try:
        ok, admin_ok, admin_total = await set_bot_commands()
        logger.info(f"[App] Command menu set on startup: default={ok}, admins={admin_ok}/{admin_total}")
    except Exception as e:
        logger.error(f"[App] Failed to set command menu on startup: {e}")
    try:
        await _recover_rapid_jobs()
    except Exception as e:
        logger.error(f"[App] /rapid job recovery failed: {e}")
