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
import difflib
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
    pdf_to_images, pdf_to_images_safe, image_to_bytes, generate_mcq_from_image,
    generate_new_mcq, parse_pdf_command, parse_page_range,
    fmt_page, gen_session_id, get_random_ayat, get_motivation,
    key_rotator, crop_explanation_image, get_pdf_page_count,
    _PDF_MAX_PAGES_PER_CALL
)

from core import (
    logger, app, sb,
    BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY, OWNER_ID,
    CF_WORKER_URL, HF_SPACE_URL, RENDER_URL, D1_TOKEN, TG_API, GH_PAGES_EXAM_URL,
    d1_set, d1_get, d1_del, d1_query, d1_select, d1_run,
    tg_post, send_msg, edit_msg, edit_msg_caption, send_photo, send_photo_by_id,
    send_document, send_poll, notify_owner, download_tg_file,
    db_get_settings, db_save_settings_field, db_is_owner_or_admin, db_track_user, db_save_session,
    db_save_mcq_cache, db_update_cache, db_get_mcq_cache,
    db_get_new_gen_count, db_increment_gen_count, db_save_leaderboard,
    db_get_channels, db_save_channel, db_delete_channel, db_rename_channel, db_save_last_quiz, db_get_last_quiz,
    build_back_url, source_msg_id,
    get_recent_errors, clear_error_logs,
    add_watermark_to_pdf,
    get_bot_username,
)

# chorcha.net mhtml/html → Premium PDF (Question Bank converter)
from chorcha_parser import parse_chorcha_file
from chorcha_pdf import build_chorcha_pdf_html

# ATLAS full mhtml/html → CSV converter (Chorcha.net + Testmoz, LaTeX cleanup, imgbb images)
# Ported 100% from AtlasMasterBot's mhtml_handler.py
from atlas_mhtml import parse_mhtml_to_mcqs, results_to_csv_bytes

# ============================================================
# mhtml/html AUTO QUEUE SYSTEM (ported from AtlasMasterBot)
# File পাঠালেই content দেখে bot নিজে সিদ্ধান্ত নেয়:
#   - MCQ format (options সহ) পাওয়া গেলে → auto CSV বানায় (queue দিয়ে, একটার পর একটা)
#   - Q&A/CQ format (options ছাড়া, চর্চা ক-ভান্ডার/খ-ভান্ডার/CQ স্টাইল) পাওয়া গেলে
#     → বলে দেয় /qpdf দিয়ে PDF বানাতে (কারণ ওটা alada page structure)
#   - দুটোর কোনোটাই না পেলে → error দেখায়
# ============================================================
_mhtml_auto_queue = asyncio.Queue()
_mhtml_worker_started = False


def _detect_mhtml_format(raw_bytes: bytes, file_name: str) -> str:
    """
    Content দেখে বলে দেয় এই file কোন pipeline এ যাবে:
      "mcq"  -> atlas_mhtml (options সহ MCQ, Chorcha.net p-5/rounded-xl বা Testmoz) -> auto CSV
      "qa"   -> chorcha_parser (Q&A/CQ, border/rounded-xl স্টাইল, options ছাড়া)   -> /qpdf বলবে
      "none" -> কোনো চেনা format পাওয়া যায়নি
    """
    try:
        parsed = parse_mhtml_to_mcqs(raw_bytes, file_name)
        if parsed["results"]:
            return "mcq"
    except Exception as e:
        logger.warning(f"[MHTML-Detect] mcq-format check failed: {e}")

    try:
        qa_data = parse_chorcha_file(raw_bytes)
        if qa_data.get("items"):
            return "qa"
    except Exception as e:
        logger.warning(f"[MHTML-Detect] qa-format check failed: {e}")

    return "none"


async def _mhtml_auto_worker():
    """Queue worker — একটার পর একটা file process করে (AtlasMasterBot এর মতোই serial queue)."""
    while True:
        msg = await _mhtml_auto_queue.get()
        try:
            await _process_mhtml_auto(msg)
        except Exception as e:
            logger.error(f"[MHTML-Worker] Error: {e}")
        finally:
            _mhtml_auto_queue.task_done()


def _mhtml_progress_bar(pct: int) -> str:
    filled = round(pct / 10)
    return "█" * filled + "░" * (10 - filled)


def _fmt_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _fmt_eta(sec: int) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    return f"{m}m {s}s"


_MHTML_PHASE_LABELS = {
    "downloading": "📥 File ডাউনলোড হচ্ছে...",
    "detecting": "🔍 Format যাচাই করা হচ্ছে...",
    "parsing": "⏳ MCQ প্রসেসিং চলছে...",
    "csv_building": "📄 CSV বানানো হচ্ছে...",
    "sending": "📤 CSV পাঠানো হচ্ছে...",
}


async def _mhtml_live_updater(job_id: str, chat_id: int, loading_id: int):
    """Edits the TG loading message every ~2s with live phase, %, ETA, done/total."""
    last_text = ""
    while True:
        job = MHTML_JOBS.get(job_id)
        if not job or job["status"] != "running":
            break
        pct = job["pct"]
        done, total = job["done"], job["total"]
        phase = job.get("phase", "parsing")
        bar = _mhtml_progress_bar(pct)
        label = _MHTML_PHASE_LABELS.get(phase, "⏳ প্রসেসিং চলছে...")
        text = f"{label}\n[{bar}] {pct}%"
        elapsed_sec = int(time.time() - job["started_at"])
        if phase == "downloading":
            dl_done = job.get("dl_done", 0)
            dl_total = job.get("dl_total", 0)
            dl_speed = job.get("dl_speed", 0)
            text += f"\n📦 {_fmt_bytes(dl_done)}/{_fmt_bytes(dl_total) if dl_total else '?'}"
            if dl_speed:
                text += f" @ {_fmt_bytes(dl_speed)}/s"
        elif phase in ("parsing", "csv_building", "sending"):
            text += f"\n📝 হয়েছে: {done}/{total if total else '?'}"
        # সব phase-এ (detecting সহ) সবসময় elapsed+ETA দেখাবে — প্রতি সেকেন্ডে
        # টেক্সট বদলায় বলে message কখনো "আটকে গেছে" মনে হবে না, live-update guaranteed
        text += f"\n⌛ শুরু: {_fmt_eta(elapsed_sec)} আগে | বাকি ETA: {_fmt_eta(job['eta_sec'])}"
        if text != last_text and loading_id:
            try:
                await edit_msg(chat_id, loading_id, text)
                last_text = text
            except Exception:
                pass
        await asyncio.sleep(1)


async def _cleanup_job_later(job_id: str, delay: int = 30):
    await asyncio.sleep(delay)
    MHTML_JOBS.pop(job_id, None)


async def _process_mhtml_auto(msg: dict):
    chat_id = msg["chat"]["id"]
    doc = msg["document"]
    file_name = doc.get("file_name", "")

    loading = await send_msg(chat_id, "🔍 File বিশ্লেষণ করা হচ্ছে...")
    loading_id = loading.get("result", {}).get("message_id")

    job_id = gen_session_id()
    MHTML_JOBS[job_id] = {
        "status": "running", "phase": "downloading", "done": 0, "total": 0, "pct": 0,
        "eta_sec": 0, "started_at": time.time(), "source": None,
        "file_name": file_name, "chat_id": chat_id, "loading_id": loading_id,
        "csv_ready": False, "error": None,
        "dl_done": 0, "dl_total": 0, "dl_speed": 0,
    }
    updater_task = asyncio.create_task(_mhtml_live_updater(job_id, chat_id, loading_id))

    try:
        _dl_start = time.time()

        def _dl_progress_cb(downloaded, total):
            job = MHTML_JOBS.get(job_id)
            if not job:
                return
            job["dl_done"] = downloaded
            job["dl_total"] = total
            job["pct"] = min(5, round((downloaded / total) * 5)) if total else 0
            elapsed = time.time() - _dl_start
            if elapsed > 0:
                speed = downloaded / elapsed
                job["dl_speed"] = speed
                if total and speed > 0:
                    job["eta_sec"] = round((total - downloaded) / speed)

        raw_bytes = await download_tg_file(doc["file_id"], _dl_progress_cb,
                                           chat_id=chat_id, message_id=msg["message_id"])
        job = MHTML_JOBS.get(job_id)
        if job:
            job["phase"] = "detecting"
            job["pct"] = 5
            job["eta_sec"] = 3

        async def _detect_ticker():
            # detect_mhtml_format ব্লকিং কল — বড় ফাইলে সময় লাগতে পারে।
            # এই ticker pct/eta কে নিজে থেকে ধীরে ধীরে বাড়ায়, যাতে user দেখতে
            # পায় প্রসেস চলছে, স্থির আটকে নেই।
            j = MHTML_JOBS.get(job_id)
            while j and j.get("phase") == "detecting":
                j["pct"] = min(15, j["pct"] + 1)
                j["eta_sec"] = max(1, j["eta_sec"] - 1)
                await asyncio.sleep(1)
                j = MHTML_JOBS.get(job_id)

        ticker_task = asyncio.create_task(_detect_ticker())
        try:
            fmt = await asyncio.to_thread(_detect_mhtml_format, raw_bytes, file_name)
        finally:
            ticker_task.cancel()

        if fmt == "qa":
            updater_task.cancel()
            MHTML_JOBS.pop(job_id, None)
            if loading_id:
                await edit_msg(chat_id, loading_id,
                    "📋 এই file-টা Q&A/CQ ফরম্যাটের (চর্চা ক-ভান্ডার/খ-ভান্ডার/CQ)!\n\n"
                    "এটার জন্য PDF বানাতে হলে এই file-এ <b>reply করে</b> "
                    "<code>/qpdf</code> কমান্ড দাও।")
            return

        if fmt == "none":
            updater_task.cancel()
            MHTML_JOBS.pop(job_id, None)
            if loading_id:
                await edit_msg(chat_id, loading_id,
                    "❌ কোনো চেনা প্রশ্ন/উত্তর ফরম্যাট খুঁজে পাওয়া যায়নি! "
                    "Format ভিন্ন হতে পারে।")
            return

        # fmt == "mcq" → CSV বানাও, লাইভ progress সহ
        if job:
            job["phase"] = "parsing"

        def _progress_cb(done, total):
            job = MHTML_JOBS.get(job_id)
            if not job:
                return
            job["phase"] = "parsing"
            job["done"] = done
            job["total"] = total
            job["pct"] = min(95, 5 + round((done / total) * 85)) if total else 5
            elapsed = time.time() - job["started_at"]
            if done > 0 and total:
                per_item = elapsed / done
                remaining = max(0, total - done)
                job["eta_sec"] = round(per_item * remaining)

        parsed = await asyncio.to_thread(parse_mhtml_to_mcqs, raw_bytes, file_name, _progress_cb)
        results = parsed["results"]
        source = parsed["source"] or "Unknown"

        job = MHTML_JOBS.get(job_id)
        if job:
            job["phase"] = "csv_building"
            job["pct"] = 95
            job["source"] = source
            job["done"] = len(results)
            job["total"] = max(job["total"], len(results))

        csv_bytes = await asyncio.to_thread(results_to_csv_bytes, results)

        if job:
            job["phase"] = "sending"
            job["pct"] = 100
            job["eta_sec"] = 0
            job["status"] = "done"
            job["csv_ready"] = True

        updater_task.cancel()

        safe_title = re.sub(r"[^\w\u0980-\u09FF\-]+", "_", file_name.rsplit(".", 1)[0])[:50] or "ATLAS_QuestionBank"
        await send_document(chat_id, csv_bytes, f"ATLAS_{safe_title}.csv",
            caption=f"📚 Source: {source}\n📝 মোট MCQ: {len(results)}\n🚀 ATLAS APP",
            mime_type="text/csv")

        if loading_id:
            await tg_post("deleteMessage", {"chat_id": chat_id, "message_id": loading_id})

        MHTML_JOBS.pop(job_id, None)

    except Exception as e:
        logger.error(f"[MHTML-Auto] Error: {e}")
        if job_id in MHTML_JOBS:
            MHTML_JOBS[job_id]["status"] = "error"
            MHTML_JOBS[job_id]["error"] = str(e)
        try:
            updater_task.cancel()
        except Exception:
            pass
        asyncio.create_task(_cleanup_job_later(job_id))
        await _safe_error_reply(chat_id, e)

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
# v-RAM-fix: cap in-memory PIL-image page caches (pdf_cache/qbm_cache) so an
# abandoned upload flow (user never finishes channel-select) can't leak heavy
# decoded images forever. Self-overwrites per uid, but this adds a hard
# ceiling + oldest-key eviction as a safety net.
_PAGE_CACHE_MAX_ENTRIES = 50

def _cap_page_cache(cache: dict) -> None:
    while len(cache) > _PAGE_CACHE_MAX_ENTRIES:
        cache.pop(next(iter(cache)), None)

# SPEED: cache raw downloaded PDF bytes by file_id so re-running /pdf on the
# SAME file (retry, different page range, re-generate) skips the Telegram
# download entirely. Small cap + oldest-eviction, same safety pattern as
# _cap_page_cache. In-memory only — resets on deploy/restart, which is fine
# since it's purely a speed optimization, never a correctness dependency.
_PDF_BYTES_CACHE_MAX = 40  # HF has 16GB RAM (not Render free tier) — safe to hold more raw PDFs
_pdf_bytes_cache = {}  # file_id -> bytes

def _cap_pdf_bytes_cache() -> None:
    while len(_pdf_bytes_cache) > _PDF_BYTES_CACHE_MAX:
        _pdf_bytes_cache.pop(next(iter(_pdf_bytes_cache)), None)

async def _download_pdf_cached(file_id: str, chat_id: int = None, message_id: int = None) -> bytes:
    cached = _pdf_bytes_cache.get(file_id)
    if cached is not None:
        logger.info(f"[PDF Cache] hit for file_id={file_id[:16]}... skipping download")
        return cached
    data = await download_tg_file(file_id, chat_id=chat_id, message_id=message_id)
    _pdf_bytes_cache[file_id] = data
    _cap_pdf_bytes_cache()
    return data

# SPEED: cache RENDERED page images by (file_id, page_range) so a re-run of
# /pdf on the same file+range skips pdf2image rasterization too (the
# heaviest CPU step). Safe now that we're on HF's 16GB RAM instance instead
# of Render's memory-constrained free tier.
_PDF_RENDER_CACHE_MAX = 15
_pdf_render_cache = {}  # (file_id, page_range) -> pages list

def _cap_pdf_render_cache() -> None:
    while len(_pdf_render_cache) > _PDF_RENDER_CACHE_MAX:
        _pdf_render_cache.pop(next(iter(_pdf_render_cache)), None)

def _render_pdf_cached(file_id: str, pdf_bytes: bytes, page_range: str = None):
    """Wraps pdf_to_images_safe with a render cache keyed by (file_id, page_range)."""
    key = (file_id, page_range or "ALL")
    cached = _pdf_render_cache.get(key)
    if cached is not None:
        logger.info(f"[Render Cache] hit for {key[0][:16]}...:{key[1]} — skipping rasterization")
        return True, cached
    ok, pages = pdf_to_images_safe(pdf_bytes, page_range)
    if ok and pages:
        _pdf_render_cache[key] = pages
        _cap_pdf_render_cache()
    return ok, pages

PIN_ENABLED = {}  # chat_id -> bool (in-memory, also saved to DB)
PDF_AUTO_ENABLED = {}  # chat_id -> bool (in-memory, also saved to DB) — /pdf on|off

# ============================================================
# /cancel — instantly stop any running activity for this chat (bot stays alive)
# ============================================================
CANCEL_FLAGS = {}  # chat_id -> bool, checked by long loops between steps
ACTIVE_JOB_LABEL = {}  # chat_id -> human-readable label of the job currently running

def is_cancelled(chat_id):
    return CANCEL_FLAGS.get(chat_id, False)

def clear_cancel(chat_id):
    CANCEL_FLAGS[chat_id] = False

def set_active_job(chat_id, label):
    ACTIVE_JOB_LABEL[chat_id] = label

def clear_active_job(chat_id):
    ACTIVE_JOB_LABEL.pop(chat_id, None)

async def handle_cancel_command(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg.get("from", {}).get("id")
    if not await db_is_owner_or_admin(uid):
        return
    CANCEL_FLAGS[chat_id] = True
    running_label = ACTIVE_JOB_LABEL.get(chat_id)
    if running_label:
        await send_msg(chat_id, "🛑 বন্ধ করা হলো।\nযে কাজ থামলো: " + running_label)
    else:
        await send_msg(chat_id, "🛑 এই মুহূর্তে এই চ্যাটে cancel-able কোনো কাজ চলছে না।")

# LIVE QUIZ CONFIG
LIVE_QUIZ_STATE = {}  # channel_id -> live quiz state
LIVE_TIMERS = {}      # channel_id -> timer task

# IMAGE COLLECTION (for /pdf image→PDF feature)
IMG_COLLECTION = {}   # uid -> {"imgs": [], "collecting": bool}

# v1.2: /watermark feature — uid -> pdf_bytes (waiting for watermark text)
WATERMARK_PENDING = {}
CHANNEL_RENAME_PENDING = {}  # uid -> channel_id awaiting new name text


# v1.3: /rapid — scheduled comment-based question drop in a channel
# uid -> {"step": "awaiting_time", "topic":..., "mcqs":..., "channel_id":...}
RAPID_PENDING = {}
# job_id -> asyncio.Task (so a scheduled /rapid run can be cancelled before it fires)
RAPID_TASKS = {}

# v1.3: /api/new-exam — async job state for instant progress page
# job_id -> {"status": "running"|"done"|"error", "pct": int, "eta_sec": int,
#            "started_at": float, "new_cache_id": str, "error": str}
NEW_EXAM_JOBS = {}

# v-mhtml-live: MHTML/HTML → CSV job state for live dashboard + live TG progress msg
# job_id -> {"status": "running"|"done"|"error", "done": int, "total": int,
#            "pct": int, "eta_sec": int, "started_at": float, "source": str,
#            "file_name": str, "chat_id": int, "loading_id": int,
#            "csv_ready": bool, "error": str}
MHTML_JOBS = {}

# v1.2: /ping status command — set at startup, used to compute uptime
BOT_START_TIME = time.time()

# DEFAULT LIVE QUIZ TIME (seconds per question)
DEFAULT_LIVE_TIME = 10

# ============================================================
# MULTI-AI MODEL ROTATION (Vision MCQ generation)
# Order: Gemini (via pdf_handler) → NVIDIA Llama 3.2 11B Vision
#        → OpenRouter Qwen2-VL 72B → Nemotron Nano Omni → Gemma → Hugging Face
# Missing keys are skipped silently — never raise.
# ============================================================
import base64 as _b64_ai
from pdf_handler import generate_mcq_from_image as _gemini_gen_mcq

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

async def _safe_error_reply(chat_id, e: Exception, context: str = ""):
    """SECURITY: never leak raw exception text to the user — log full detail,
    notify owner privately, and show the user a generic Bengali fallback."""
    logger.error(f"[ERROR]{f' [{context}]' if context else ''}: {e}", exc_info=True)
    try:
        await notify_owner(f"🚨 QuizBot ERROR{f' [{context}]' if context else ''}:\n{e}"[:4000])
    except Exception:
        pass
    await send_msg(chat_id, "❌ কিছু একটা সমস্যা হয়েছে। একটু পর আবার চেষ্টা করুন।")


def _build_mcq_prompt(topic: str, count) -> str:
    count_min = count_max = None
    if isinstance(count, (tuple, list)) and len(count) == 2:
        count_min, count_max = count[0], count[1]
        count = None
    if count_min and count_max:
        count_rule = (
            f"STRICT RANGE REQUIRED: Extract BETWEEN {count_min} AND {count_max} MCQs "
            f"from this page — never fewer than {count_min}, never more than {count_max}. "
            f"If the page doesn't have enough distinct information for {count_min}, "
            f"get as close as possible by rephrasing/re-angling the same facts from "
            f"different angles (different question style, different correct option "
            f"position) — never stop early just because it feels repetitive. If the "
            f"page has more content than {count_max} MCQs worth, pick the {count_max} "
            f"most important/highest-priority facts (highlighted/marked content first)."
        )
    elif count:
        count_rule = (
            f"EXACT COUNT REQUIRED: Extract exactly {count} MCQs from this page. "
            f"If the page genuinely doesn't have enough distinct information for "
            f"{count}, get as close as possible by rephrasing/re-angling the same "
            f"facts from different angles (different question style, different "
            f"correct option position) — never stop early just because it feels repetitive."
        )
    else:
        count_rule = (
            "TARGET ~15 MCQs REQUIRED (no fixed number given by user, default target): "
            "Extract AT LEAST 15 quality MCQs from every piece of information on this "
            "page, more if the page is content-rich (up to 35). Re-angle the same facts "
            "into different question styles (direct fact, true/false-style, definition, "
            "comparison, cause-effect, fill-in-the-blank) so nothing usable on the page "
            "is left unused — do NOT stop at 6-10 just because the 'obvious' MCQs ran "
            "out; that is under-extracting. Only go below 15 if the page has genuinely "
            "very little text (then at least 10, minimum 5 if truly sparse)."
        )
    return (
        f"You are an expert MCQ-extraction engine for Bengali/English academic "
        f"textbook pages (medical/HSC/admission-standard quality).\n"
        f"Topic: {topic}\n\n"

        f"═══════════════════════════════\n"
        f"🟥 OVERALL RULES\n"
        f"═══════════════════════════════\n"
        f"- Whether the image already looks like ready-made MCQs or plain "
        f"information, generate MCQs from EVERY part of it — nothing is off-limits.\n"
        f"- {count_rule}\n"
        f"- Never make junk/filler MCQs just to hit a number — if you must reuse "
        f"the same fact to reach the target count, rephrase it into a genuinely "
        f"different question angle, not a copy-paste.\n"
        f"- NEVER generate MCQs from topic names, chapter titles, headlines, or "
        f"page numbers — these are structural labels, not content.\n"
        f"- Among the MCQs generated, 3-5 of them should mix several distinct "
        f"facts from the page into a single question — e.g. options that are each "
        f"a combination of 2-3 facts, where only one option has ALL facts correct. "
        f"Keep these moderate difficulty (not extreme/confusing) — a student who "
        f"reads carefully should be able to solve it.\n\n"

        f"═══════════════════════════════\n"
        f"🟥 MUST-PRIORITY — NEVER skip lines that are marked in ANY way\n"
        f"═══════════════════════════════\n"
        f"- Any line/paragraph highlighted or marked with ANY color (green, red, "
        f"orange, yellow are the most common highlighter colors)\n"
        f"- Any paragraph/line boxed, circled, or color-marked\n"
        f"- Any line underlined with pen ink (red, black, blue, green — any color)\n"
        f"- ANY extra color, mark, box, star, or underline added on top of the "
        f"book's original printed line means this content is high-priority and "
        f"you MUST make an MCQ from it — do not skip it under any circumstance.\n"
        f"- Tables/charts get special priority — use every cell of information "
        f"in them for MCQs, don't just describe the table.\n\n"

        f"═══════════════════════════════\n"
        f"🟥 বক্স/ছক STYLE INFO — MANDATORY NEAR-FULL COVERAGE\n"
        f"═══════════════════════════════\n"
        f"If the page contains info laid out in boxes/ছক (bordered boxes, "
        f"info-cards, terms-in-boxes, ছক/সারণি cells, or any visually separated "
        f"box-style content blocks):\n"
        f"- You MUST generate at least one MCQ from EVERY box/ছক cell on the page, "
        f"EXCEPT at most 2-3 boxes that are genuinely too trivial/empty/purely "
        f"decorative to support a real question — skipping more than 2-3 boxes "
        f"is NOT allowed.\n"
        f"- If a box contains rich/dense information (multiple facts, sub-points, "
        f"comparisons), generate MORE THAN ONE MCQ from that single box — as many "
        f"as the information genuinely supports, up to a HIGHEST cap of 15 MCQs "
        f"from any single box if that box is unusually information-dense.\n"
        f"- Do not treat boxes as a single combined source — walk through them "
        f"one by one and make sure each one is individually represented in the "
        f"output, not just the first few.\n\n"

        f"═══════════════════════════════\n"
        f"🟦 QUESTION-ANGLE VARIATION\n"
        f"═══════════════════════════════\n"
        f"Never repeatedly start questions by naming the topic itself in the same "
        f"pattern (e.g. always \"X সম্পর্কে কোনটি সঠিক?\", \"X এর গঠন কী?\", \"X এর ক্ষেত্রে...\" "
        f"back to back) — this reads as boring/repetitive. Use varied question "
        f"structures: direct fact, definition, cause-effect, comparison, fill-in-"
        f"the-blank, \"কোনটি সঠিক নয়\" style, etc. Mix them naturally across the set.\n"
        f"Occasionally reverse the direction of a fact-based question instead of "
        f"always asking it the same way — e.g. if one MCQ asks 'বাংলাদেশের রাজধানী "
        f"কোথায়?' (answer: ঢাকা), elsewhere in the set also ask the reverse angle "
        f"like 'ঢাকা কোন দেশের রাজধানী?' (answer: বাংলাদেশ) where the same fact "
        f"supports it. Don't do this for every fact — just mix it in naturally "
        f"for facts that genuinely have a sensible reverse phrasing, so the set "
        f"isn't monotonous.\n\n"

        f"═══════════════════════════════\n"
        f"💥 প্রশ্ন (QUESTION)\n"
        f"═══════════════════════════════\n"
        f"- Short and clear (roughly 1-1.5-2 lines), never needlessly wordy or convoluted.\n"
        f"- Cover every plausible angle the source content could be tested on, but "
        f"never invent facts not present in the source image.\n"
        f"- Should not be unnecessarily hard/tricky — quality and clarity matter more "
        f"than difficulty.\n"
        f"- 🚨 EXACT TERM FIDELITY (CRITICAL): Copy every proper noun, name, place, "
        f"country, term, or spelling EXACTLY as printed on the page — character "
        f"for character. NEVER substitute a similar-looking or similar-sounding "
        f"word (e.g. if the page says 'তুরস্ক', never write 'তুর্ক'; if it says "
        f"'উসমানীয়', never write 'অটোমান' unless the page itself uses that word). "
        f"Before finalizing each question/option/explanation, re-check every proper "
        f"noun against the source image's actual spelling — do not rely on memory "
        f"or general knowledge for how a name is 'usually' spelled.\n\n"

        f"═══════════════════════════════\n"
        f"💥 অপশন (OPTIONS) — exactly 4\n"
        f"═══════════════════════════════\n"
        f"- Size: options are generally short (often a single word/term), with some "
        f"variation — roughly a 20% mix of longer options (a short phrase) is fine; "
        f"don't force every option to the exact same length artificially.\n"
        f"- All 4 options must be filled with real, substantive content from the "
        f"source — never single-word filler like 'yes/no/true/false' as an option.\n"
        f"- Not limited to one specific topic/box on the page — mix in related "
        f"information from elsewhere in the same source as distractors, that's fine.\n"
        f"- Prefer distractors that are CLOSE/CONFUSABLE with the correct answer "
        f"(pulled from nearby/related information in the same source — HIGH PRIORITY), "
        f"so a student actually has to know the material rather than guess by "
        f"elimination, and genuinely has to think about which option is correct.\n"
        f"- Exactly ONE option must be correct — verify there is no ambiguity "
        f"where two options could both be defended as correct.\n\n"

        f"═══════════════════════════════\n"
        f"💥 উত্তর (ANSWER)\n"
        f"═══════════════════════════════\n"
        f"- Exactly one of A/B/C/D.\n"
        f"- Give the highest priority to making sure more than one option can never "
        f"be defended as correct — double-check this for every single MCQ.\n"
        f"- Across the full set of MCQs you generate, vary WHICH option letter is "
        f"correct from question to question (don't let the correct answer always "
        f"land on the same letter/position) — the answer's position must be genuinely "
        f"determined by where the correct option ended up, not forced into a pattern.\n\n"

        f"═══════════════════════════════\n"
        f"💥 ব্যাখ্যা (EXPLANATION) — TWO PARTS, BOTH MANDATORY, NO EXCEPTIONS\n"
        f"═══════════════════════════════\n"
        f"An explanation that ONLY names/restates the correct answer (a single "
        f"short line like 'সঠিক উত্তর X' or 'The answer is X') is INVALID and "
        f"REJECTED — that is not an explanation, that is just repeating the answer. "
        f"Every explanation MUST contain BOTH of the following parts, in this order:\n"
        f"  PART 1 (answer confirmation): State which option is correct.\n"
        f"  PART 2 (source-derived surrounding context — MUST, never optional): "
        f"Add 1-2 sentences of ADDITIONAL related facts/details pulled directly "
        f"from the same source image — information near/around this specific "
        f"fact on the page (nearby lines, the same paragraph, a related row in "
        f"the same table, a definition/number/date/name that appears close to "
        f"this fact in the source), so that solving this MCQ and reading the "
        f"explanation teaches the student a bit more than just the bare answer. "
        f"This must be genuinely new information beyond the bare answer — not a "
        f"restatement of the question, not a restatement of the correct option's "
        f"text, not a generic filler sentence.\n"
        f"- If you cannot find any nearby related fact in the source for part 2, "
        f"you MUST look again at the surrounding lines/paragraph/table before "
        f"giving up — nearly every source page has at least one adjacent fact "
        f"(a number, a name, a related term, a cause/effect, a definition) that "
        f"can serve as part 2. Only if the source page is truly a single isolated "
        f"fact with absolutely nothing else nearby may part 2 be a brief factual "
        f"elaboration on the answer itself, still from the source, never invented.\n"
        f"- Everything in both parts must come from the source image — never "
        f"introduce outside facts, never guess, never use general knowledge not "
        f"visible in the source.\n"
        f"- Self-check before finalizing EVERY explanation: if it reads as one "
        f"short clause with no additional fact beyond naming the answer, it FAILS "
        f"this rule — rewrite it to add the required part-2 context before output.\n"
        f"- Length: roughly 100-200 characters — long enough to fit both parts, "
        f"but still concise.\n\n"

        f"═══════════════════════════════\n"
        f"🟩 STRICT LANGUAGE RULE\n"
        f"═══════════════════════════════\n"
        f"Detect the language of the source image text (Bengali or English) and "
        f"write the question, ALL options, and the explanation in that exact same "
        f"language. Never translate — if the source is English, output English; "
        f"if the source is Bengali, output Bengali.\n\n"

        f"═══════════════════════════════\n"
        f"🚫 FORBIDDEN SOURCE-REFERENCE PHRASES (question AND explanation, always)\n"
        f"═══════════════════════════════\n"
        f"NEVER use phrases that refer back to the source material itself instead of "
        f"stating the fact directly — in the question OR the explanation:\n"
        f"❌ \"টপিকে বলা হয়েছে\" / \"দেখা যাচ্ছে\" / \"লিখা আছে\" / \"বর্ণিত আছে\" / \"উল্লেখ আছে\" / "
        f"\"চিত্রে দেখা যাচ্ছে\" / \"বক্সে\" / \"ছকে\" / \"সারণিতে\" / \"পৃষ্ঠায়\" / \"প্রদত্ত অংশে\" / "
        f"\"উপরে দেখানো\" / \"টেক্সট অনুসারে\" / \"টেক্সটে লিখা আছে\"\n"
        f"❌ English equivalents: \"as shown in the figure/box/table\", \"mentioned in the "
        f"text/page\", \"as given above\", \"according to the source\"\n"
        f"Instead: ALWAYS state the actual fact directly and plainly, as if it were "
        f"general knowledge — never mention or imply it came from \"the shown image/box/"
        f"table/page\". Applies to every single MCQ's question and explanation, no exceptions.\n\n"

        f"For EACH MCQ, also give 'exp_bbox': a TIGHT bounding box centered exactly "
        f"on the specific line/paragraph/table this MCQ's answer came from — include "
        f"ONLY that fact/content with minimal margin (just enough to show the full "
        f"sentence/row, not neighboring unrelated facts or other MCQs' content). "
        f"The bbox must be centered on the source text vertically and horizontally, "
        f"not offset toward unrelated page content. "
        f"Normalize to 0-1000 scale ([x_min,y_min,x_max,y_max], top-left=[0,0], "
        f"bottom-right=[1000,1000]). If unsure, use null.\n\n"
        f"Return STRICT JSON array only, no prose, no markdown fences. Schema:\n"
        f"[{{\"question\":\"...\",\"options\":[\"A\",\"B\",\"C\",\"D\"],"
        f"\"answer\":\"A|B|C|D\",\"explanation\":\"...\",\"exp_bbox\":[100,200,900,350]}}]"
    )

def _strip_q_numbering(q: str) -> str:
    """
    Question টেক্সটের শুরুতে যেকোনো ধরনের numbering prefix (1) 14) 1. Q1. Q.1
    ক) ১) ইত্যাদি) সরিয়ে দেয়, প্রশ্নের বাকি অংশ অক্ষত রেখে। AI prompt-এ নিষেধ
    থাকা সত্ত্বেও মাঝেমধ্যে numbering দিয়ে ফেলে — এই safety-net সব MCQ সোর্সে
    (/pdf, /img, /txt, /poll) parse-time এ প্রয়োগ হয় যাতে কখনো miss না যায়।
    """
    if not q:
        return q
    # ইংরেজি/বাংলা সংখ্যা + ঐচ্ছিক ) . । স্পেস — শুরুতে একবার বা দুইবার (Q1. এর মতো)
    pattern = r'^\s*(?:[Qq]\.?\s*)?[\d১২৩৪৫৬৭৮৯০]{1,3}\s*[).।:.\-]\s*'
    prev = None
    cur = q
    # চেইনড prefix (যেমন "Q1) 2." ভুলে দুইবার) ধরার জন্য সর্বোচ্চ ২ বার strip
    for _ in range(2):
        new = re.sub(pattern, '', cur)
        if new == cur:
            break
        cur = new
    return cur.strip()

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
            q = _strip_q_numbering(q)
            opts = it.get("options") or it.get("opts") or []
            if not q or not isinstance(opts, list) or len(opts) < 2:
                continue
            opts = [str(o)[:300] for o in opts][:4]
            ans = str(it.get("answer", "A")).strip().upper()
            if ans in ("1","2","3","4"):
                ans = {"1":"A","2":"B","3":"C","4":"D"}[ans]
            if ans not in ("A","B","C","D"):
                ans = "A"
            if any(re.match(r'^(card|page|section|chapter|part|topic|slide)\s*\d*$', str(o).strip(), re.IGNORECASE) for o in opts):
                continue
            bbox = it.get("exp_bbox")
            if not (isinstance(bbox, list) and len(bbox) == 4):
                bbox = None
            out.append({
                "question": q,
                "options": opts,
                "answer": ans,
                "explanation": str(it.get("explanation",""))[:500],
                "exp_bbox": bbox,
            })
    return out

async def _post_openai_compat(url: str, key: str, model: str, data_url: str, prompt: str) -> tuple:
    """Returns (text, status_code). status_code=0 means network/exception (no HTTP response)."""
    if not key:
        return "", 0
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
        "max_tokens": 8192,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(url, headers=headers, json=payload)
            if r.status_code >= 400:
                logger.warning(f"[AI-ROT] {model} HTTP {r.status_code}: {r.text[:200]}")
                return "", r.status_code
            j = r.json()
            return j.get("choices", [{}])[0].get("message", {}).get("content", "") or "", r.status_code
    except Exception as e:
        logger.warning(f"[AI-ROT] {model} err: {e}")
        return "", 0

class GroqKeyRotator:
    """Rotates across multiple Groq keys (GROQ_KEYS comma-separated, falls back
    to single GROQ_API_KEY). On quota/rate-limit (429) for one key, tries the
    next key immediately within the same page-call before giving up to Gemini."""
    def __init__(self):
        self.keys = []
        self.current = 0
        self._load_keys()

    def _load_keys(self):
        raw = os.environ.get("GROQ_KEYS", "") or os.environ.get("GROQ_API_KEY", "")
        if raw:
            self.keys = [k.strip() for k in raw.split(",") if k.strip()]
        logger.info(f"[Groq] Loaded {len(self.keys)} keys")

    def all_keys(self):
        if not self.keys:
            self._load_keys()
        return list(self.keys)

groq_key_rotator = GroqKeyRotator()

async def _gen_groq(img, topic, count):
    keys = groq_key_rotator.all_keys()
    if not keys:
        return []
    data_url = _img_to_data_url(img)
    if not data_url:
        return []
    prompt = _build_mcq_prompt(topic, count)
    # meta-llama/llama-4-scout-17b-16e-instruct was deprecated by Groq on
    # 2026-06-17 (see console.groq.com/docs/deprecations) — every call to it
    # now fails, which silently fell through to Gemini. qwen/qwen3.6-27b is
    # Groq's current vision-capable replacement (openai/gpt-oss-120b, their
    # other suggested replacement, is text-only and can't process images).
    for i, key in enumerate(keys):
        txt, status = await _post_openai_compat(
            "https://api.groq.com/openai/v1/chat/completions",
            key, "qwen/qwen3.6-27b",
            data_url, prompt
        )
        if txt:
            return _parse_mcq_json(txt)
        if status == 429:
            logger.warning(f"[Groq] key #{i+1}/{len(keys)} quota exhausted (429), trying next key")
            continue
        # Any other error (400/404/5xx/network) — log and still try the next
        # key instead of giving up immediately, since a single bad key/model
        # response shouldn't block the whole provider when others might work.
        logger.warning(f"[Groq] key #{i+1}/{len(keys)} failed (status={status}), trying next key")
    return []

async def _gen_nvidia(img, topic, count):
    key = os.environ.get("NVIDIA_API_KEY", "")
    if not key:
        return []
    data_url = _img_to_data_url(img)
    if not data_url:
        return []
    txt, _st = await _post_openai_compat(
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
    txt, _st = await _post_openai_compat(
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
    txt, _st = await _post_openai_compat(
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
    txt, _st = await _post_openai_compat(
        "https://openrouter.ai/api/v1/chat/completions",
        key, "google/gemma-3-27b-it",
        data_url, _build_mcq_prompt(topic, count)
    )
    return _parse_mcq_json(txt)

async def _gen_hf(img, topic, count):
    """Hugging Face Inference API — free tier vision fallback (last resort)."""
    key = os.environ.get("HF_API_KEY", "")
    if not key:
        return []
    data_url = _img_to_data_url(img)
    if not data_url:
        return []
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    model = os.environ.get("HF_VISION_MODEL", "meta-llama/Llama-3.2-11B-Vision-Instruct")
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _build_mcq_prompt(topic, count)},
                {"type": "image_url", "image_url": {"url": data_url}}
            ]
        }],
        "max_tokens": 8192,
        "temperature": 0.3,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://router.huggingface.co/v1/chat/completions",
                headers=headers, json=payload
            )
            if r.status_code >= 400:
                logger.warning(f"[AI-ROT] hf HTTP {r.status_code}: {r.text[:200]}")
                return []
            j = r.json()
            txt = j.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    except Exception as e:
        logger.warning(f"[AI-ROT] hf err: {e}")
        return []
    return _parse_mcq_json(txt)

_AI_PROVIDERS_ORDER = ["nvidia", "openrouter_qwen", "nemotron", "gemma", "hf"]

_AI_FALLBACK_FNS = {
    "nvidia":          _gen_nvidia,
    "openrouter_qwen": _gen_openrouter_qwen,
    "nemotron":        _gen_nemotron,
    "gemma":           _gen_gemma,
    "hf":              _gen_hf,
}

_THIN_EXPLANATION_PATTERNS = [
    r'^সঠিক\s*উত্তর\s*[:\-—]?\s*[a-dA-D১-৪]?\s*[.।]?$',
    r'^উত্তর\s*[:\-—]?\s*[a-dA-D১-৪]?\s*[.।]?$',
    r'^the\s*(correct\s*)?answer\s*is\s*[a-d]?\.?$',
    r'^answer\s*[:\-—]\s*[a-d]\.?$',
    r'^option\s*[a-d]\s*is\s*(the\s*)?correct\.?$',
]

def _is_thin_explanation(exp: str) -> bool:
    """
    Detects an explanation that ONLY names the answer (Part 1) with no
    source-derived surrounding context (Part 2) — heuristic check used to
    catch cases where the model ignored the two-part explanation rule.
    """
    e = (exp or "").strip()
    if not e:
        return True
    # strip any already-attached <img> tag before judging length/content
    e_no_img = re.sub(r'<img[^>]*>', '', e, flags=re.IGNORECASE).strip()
    if not e_no_img:
        return True
    if len(e_no_img) < 40:  # a genuine 2-part explanation rarely fits under this
        return True
    for pat in _THIN_EXPLANATION_PATTERNS:
        if re.match(pat, e_no_img, re.IGNORECASE):
            return True
    return False

async def _repair_thin_explanations(mcqs: list, img, topic: str) -> list:
    """
    Targeted, single-pass repair for MCQs whose explanation only names the
    answer with no source-derived context — re-asks the model for JUST the
    explanation of those specific questions (not the whole page again), with
    an even stricter reminder of the two-part rule. Falls back to leaving the
    original explanation untouched if the repair call fails or still comes
    back thin (never blocks the MCQ from being delivered).
    """
    thin = [m for m in (mcqs or []) if _is_thin_explanation(m.get("explanation", ""))]
    if not thin:
        return mcqs
    questions_block = "\n".join(
        f'{i+1}. Q: {m["question"]}\n   Correct option: {m["options"][{"A":0,"B":1,"C":2,"D":3}.get(m["answer"],0)]}'
        for i, m in enumerate(thin)
    )
    repair_prompt = (
        f"Topic: {topic}\n"
        f"For EACH numbered question below, write a proper explanation using the "
        f"attached source image. A valid explanation has TWO mandatory parts: "
        f"(1) confirm the correct option, (2) add 1-2 sentences of ADDITIONAL "
        f"related facts pulled from NEAR this fact in the source image (a nearby "
        f"line, a related row, a nearby number/name/date) — never just repeat "
        f"the answer alone, never invent facts outside the source. Same language "
        f"as the source (Bengali or English). ~100-200 characters each.\n\n"
        f"{questions_block}\n\n"
        f"Return STRICT JSON array only, same order, no prose: "
        f'[{{"explanation":"..."}}, ...]'
    )
    try:
        fixed_txt = await _gen_groq_raw_text(img, repair_prompt)
        fixed = _parse_explanation_only_json(fixed_txt)
        if fixed and len(fixed) == len(thin):
            for m, new_exp in zip(thin, fixed):
                if new_exp and not _is_thin_explanation(new_exp):
                    m["explanation"] = new_exp
    except Exception as e:
        logger.warning(f"[ExplanationRepair] failed, keeping originals: {e}")
    return mcqs

def _parse_explanation_only_json(text: str) -> list:
    if not text:
        return []
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    a, b = s.find("["), s.rfind("]")
    if a != -1 and b != -1 and b > a:
        s = s[a:b+1]
    try:
        data = json.loads(s)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(it.get("explanation", "")).strip()[:500] if isinstance(it, dict) else "" for it in data]

async def _gemini_verify_raw_text(img, prompt: str) -> str:
    """Same job as _gen_groq_raw_text but via Gemini — used to get a second,
    provider-diverse opinion on missed-content verification. Best-effort:
    empty string on any failure, never raises (caller treats empty as
    'nothing extra found')."""
    try:
        if not key_rotator.keys:
            return ""
        from google import genai as gai
        from google.genai import types
        from pdf_handler import image_to_base64
        key = key_rotator.get_key()
        client = gai.Client(api_key=key)
        img_b64 = image_to_base64(img)

        def _call():
            return client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    types.Part.from_text(text=prompt),
                    types.Part.from_bytes(data=base64.b64decode(img_b64), mime_type="image/jpeg")
                ]
            )
        response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=20)
        return response.text or ""
    except Exception as e:
        logger.warning(f"[GeminiVerify] failed: {e}")
        return ""

async def _gen_groq_raw_text(img, prompt: str) -> str:
    keys = groq_key_rotator.all_keys()
    if not keys:
        return ""
    data_url = _img_to_data_url(img)
    if not data_url:
        return ""
    for key in keys:
        txt, status = await _post_openai_compat(
            "https://api.groq.com/openai/v1/chat/completions",
            key, "qwen/qwen3.6-27b",
            data_url, prompt
        )
        if txt:
            return txt
        if status != 429:
            logger.warning(f"[GroqVerify] key failed (status={status}), trying next key")
    return ""

async def generate_mcq_from_image(img, topic, page_num, mcq_count=None):
    """
    Smart wrapper: Groq first (primary), then Gemini (internal key rotation via pdf_handler).
    On failure → rotate through NVIDIA / OpenRouter Qwen VL / Nemotron / Gemma.
    Missing API keys are skipped silently. Never raises.
    """
    out = await _generate_mcq_from_image_raw(img, topic, page_num, mcq_count)
    out = _cap_mcq_options(out, 4)
    _rng_max = mcq_count[1] if isinstance(mcq_count, (tuple, list)) and len(mcq_count) == 2 else None
    if _rng_max and len(out) > _rng_max:
        out = out[:_rng_max]
    # Repair (fixes thin explanations on existing MCQs) and Verify (finds
    # MCQs call-1 missed) touch disjoint data — run concurrently instead of
    # sequentially to cut per-page latency roughly in half. Verify appends
    # new items to `out`, so run it first into a temp var and merge after.
    repair_task = asyncio.create_task(_repair_thin_explanations(list(out), img, topic))
    verify_task = asyncio.create_task(_verify_and_fix_page(list(out), img, topic, page_num, mcq_count))
    repaired, verified = await asyncio.gather(repair_task, verify_task)
    # verified = original out + any newly-found MCQs (appended at the end).
    # Rebuild: take repaired explanations for the original items, then append
    # whatever new items verify found.
    n_orig = len(out)
    merged = repaired[:n_orig] if len(repaired) >= n_orig else repaired
    merged = merged + verified[n_orig:]
    if _rng_max and len(merged) > _rng_max:
        merged = merged[:_rng_max]
    return merged


async def _verify_and_fix_page(mcqs: list, img, topic: str, page_num, mcq_count=None) -> list:
    """
    2ND CALL — verification pass over what CALL 1 produced. Checks:
      (a) did call 1 miss any highlighted/underlined/marked/boxed content,
      (b) is the MCQ count reasonably close to target (~15/page avg),
      (c) did call 1 follow the prompt rules (real info only, no junk).
    If verification finds gaps, requests ADDITIONAL mcqs only for the missed
    content (never re-generates the whole page) and appends them. Best-effort:
    any failure here just returns the original call-1 output untouched.
    """
    try:
        count_min = count_max = None
        if isinstance(mcq_count, (tuple, list)) and len(mcq_count) == 2:
            count_min, count_max = mcq_count[0], mcq_count[1]
            count_target = count_max
        else:
            count_target = mcq_count if mcq_count else 15
        current_n = len(mcqs or [])
        # If a strict range was given and we're already at/above max, don't
        # ask verify to add more — would violate the user's upper bound.
        if count_max and current_n >= count_max:
            return mcqs
        existing_qs = "\n".join(f"- {m.get('question','')[:100]}" for m in (mcqs or [])[:40])
        max_extra_note = (
            f"\nHARD CAP: do not return more than {count_max - current_n} additional "
            f"MCQ(s) — the page's total must not exceed {count_max}."
            if count_max is not None else ""
        )
        verify_prompt = (
            f"You are STRICTLY auditing MCQ coverage for this page (Topic: {topic}).\n"
            f"CALL 1 already extracted {current_n} MCQs (target ~{count_target}/page).{max_extra_note} "
            f"Existing questions already covered:\n{existing_qs or '(none)'}\n\n"
            f"MANDATORY SYSTEMATIC SCAN (do this before answering, not optional):\n"
            f"- Mentally divide the page into regions (top/middle/bottom, and left/right "
            f"if multi-column) and check EACH region separately against the list above.\n"
            f"- Pay special attention to the LAST paragraph/box/row on the page and the "
            f"BOTTOM of the page — these are the most commonly missed areas.\n"
            f"- Check every highlighted/underlined/marked/boxed/ছক item individually — "
            f"if it's not represented above, it was missed.\n"
            f"- Check every table/ছক cell individually, not just at a glance.\n"
            f"Only after this region-by-region pass, decide:\n"
            f"1. Any highlighted/underlined/marked/boxed/ছক content NOT yet covered above?\n"
            f"2. Any distinct real-info fact on the page not yet turned into an MCQ?\n"
            f"3. Is {current_n} clearly below what the page's content could support?\n\n"
            f"If you find genuinely MISSING content, return ONLY the ADDITIONAL new MCQs "
            f"(never duplicate the existing ones above) as a STRICT JSON array matching this "
            f"schema: [{{\"question\":\"...\",\"options\":[\"...\",\"...\",\"...\",\"...\"],"
            f"\"answer\":\"A\",\"explanation\":\"...\"}}]. "
            f"If nothing is missing and coverage is already complete, return exactly: []\n"
            f"Never invent facts not present on the page. No prose, JSON only."
        )
        # Two independent verify passes from DIFFERENT providers (Groq +
        # Gemini) run concurrently — genuine second-opinion diversity catches
        # misses a single model's blind spots would let through, at the cost
        # of one extra parallel call (not sequential, so latency impact is
        # small since both run at once).
        groq_task = asyncio.create_task(_gen_groq_raw_text(img, verify_prompt))
        gemini_task = asyncio.create_task(_gemini_verify_raw_text(img, verify_prompt))
        extra_txt, extra_txt_gemini = await asyncio.gather(groq_task, gemini_task)

        extra = _parse_mcq_json(extra_txt) if extra_txt else []
        extra_g = _parse_mcq_json(extra_txt_gemini) if extra_txt_gemini else []

        combined_extra = list(extra)
        if extra_g:
            existing_q_texts = {(m.get("question") or "").strip().lower()[:60] for m in combined_extra}
            for m in extra_g:
                qk = (m.get("question") or "").strip().lower()[:60]
                if qk and qk not in existing_q_texts:
                    combined_extra.append(m)
                    existing_q_texts.add(qk)

        if combined_extra:
            combined_extra = _cap_mcq_options(combined_extra, 4)
            logger.info(f"[Verify] page {page_num}: call-2 added {len(combined_extra)} missed MCQs (groq={len(extra)}, gemini_extra={len(combined_extra)-len(extra)})")
            mcqs = (mcqs or []) + combined_extra
            # Code-level hard cap: AI can still overshoot the prompt's max
            # instruction, so trim here to guarantee the strict range is
            # never violated regardless of model compliance.
            if count_max and len(mcqs) > count_max:
                mcqs = mcqs[:count_max]
    except Exception as e:
        logger.warning(f"[Verify] page {page_num} verification skipped: {e}")
    return mcqs



def _ocr_bbox_lookup(img, mcqs: list) -> dict:
    """
    ZERO extra AI/API cost — runs Tesseract OCR once on the page (local CPU,
    already installed in the Dockerfile), groups words into lines with their
    pixel bounding boxes, then fuzzy-matches each MCQ's question text against
    those OCR lines to find where on the page it came from.
    Returns {question_text: [x_min,y_min,x_max,y_max]} normalized 0-1000,
    for whatever it can confidently match (skips low-confidence matches).
    """
    if not mcqs:
        return {}
    try:
        import pytesseract
        w, h = img.size
        try:
            data = pytesseract.image_to_data(img, lang="ben+eng", output_type=pytesseract.Output.DICT)
        except pytesseract.TesseractError as te:
            # ben traineddata not installed yet on this running container
            # (e.g. before a fresh deploy picks up the updated Dockerfile) —
            # fall back to English-only rather than crashing the whole pass.
            logger.warning(f"[OCRBBoxLookup] ben+eng unavailable ({te}); falling back to eng")
            data = pytesseract.image_to_data(img, lang="eng", output_type=pytesseract.Output.DICT)

        # Group words into lines using (block, par, line) keys, keep bbox + text per line
        lines = {}
        n = len(data.get("text", []))
        for i in range(n):
            word = (data["text"][i] or "").strip()
            if not word:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            x, y, ww, hh = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            if key not in lines:
                lines[key] = {"text": [], "x0": x, "y0": y, "x1": x + ww, "y1": y + hh}
            L = lines[key]
            L["text"].append(word)
            L["x0"] = min(L["x0"], x)
            L["y0"] = min(L["y0"], y)
            L["x1"] = max(L["x1"], x + ww)
            L["y1"] = max(L["y1"], y + hh)

        line_list = [(" ".join(v["text"]), v) for v in lines.values() if v["text"]]
        if not line_list:
            return {}

        out = {}
        for m in mcqs:
            q = (m.get("question") or "").strip()
            if not q:
                continue
            # Match against the EXPLANATION text, not the question — the
            # explanation's supporting evidence (a law/definition/paragraph)
            # is often located elsewhere on the page than the question line
            # itself, so matching on the question was marking the wrong spot.
            exp_text = re.sub(r'<img\b[^>]*>', '', m.get("explanation") or "", flags=re.IGNORECASE).strip()
            q_key = (exp_text or q)[:80]
            best_ratio = 0.0
            best_box = None
            best_idx = None
            # Find the best matching single line, then extend a small window
            # around it (question text usually spans 1-3 OCR lines).
            for idx, (ltext, lbox) in enumerate(line_list):
                ratio = difflib.SequenceMatcher(None, q_key.lower(), ltext.lower()).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_box = lbox
                    best_idx = idx
            # STRICT REQUIREMENT: the explanation image must ALWAYS show the
            # source line — never skip to an unmarked fallback. Try matching
            # both the explanation text and the question text, keep whichever
            # scores higher, and use it regardless of confidence (best-effort
            # is still far better than no mark at all).
            q_text_key = q[:80]
            best_ratio_q = 0.0
            best_box_q = None
            best_idx_q = None
            for idx, (ltext, lbox) in enumerate(line_list):
                ratio = difflib.SequenceMatcher(None, q_text_key.lower(), ltext.lower()).ratio()
                if ratio > best_ratio_q:
                    best_ratio_q = ratio
                    best_box_q = lbox
                    best_idx_q = idx
            if best_ratio_q > best_ratio:
                best_ratio = best_ratio_q
                best_box = best_box_q
                best_idx = best_idx_q
            if best_box is None:
                continue  # no OCR lines at all on this page — nothing to mark

            # Extend window: include 1 line before/after to capture full context
            x0, y0, x1, y1 = best_box["x0"], best_box["y0"], best_box["x1"], best_box["y1"]
            for j in (best_idx - 1, best_idx + 1):
                if 0 <= j < len(line_list):
                    _, lb = line_list[j]
                    x0 = min(x0, lb["x0"]); y0 = min(y0, lb["y0"])
                    x1 = max(x1, lb["x1"]); y1 = max(y1, lb["y1"])

            # Normalize to 0-1000 scale (same convention as exp_bbox elsewhere)
            bbox_norm = [
                int((x0 / w) * 1000), int((y0 / h) * 1000),
                int((x1 / w) * 1000), int((y1 / h) * 1000)
            ]
            out[q] = bbox_norm
        return out
    except Exception as e:
        logger.warning(f"[OCRBBoxLookup] failed: {e}")
        return {}


async def _attach_explanation_images_if_missing(mcqs: list, img) -> list:
    """
    সব image-mode AI provider (Groq/NVIDIA/Qwen/Nemotron/Gemma/Gemini) একই জায়গা
    দিয়ে যায় এখানে — যাদের explanation-এ এখনো <img> tag নাই (Gemini path আগেই
    attach করে ফেলে, তাই সেগুলো স্কিপ হবে) তাদের exp_bbox থাকলে crop+upload করে।

    Non-Gemini providers rarely return a usable exp_bbox (prompt-only bbox
    instructions aren't reliable for them) — for those MCQs, a ZERO-cost local
    OCR + fuzzy-text-match pass (Tesseract, already installed) locates the
    source region instead of any extra AI call. If OCR match confidence is
    too low, the FULL page image is attached instead of leaving explanation
    with no visual reference at all.
    """
    try:
        from pdf_handler import crop_explanation_image, image_to_base64
        from atlas_mhtml import upload_to_imgbb
    except Exception:
        return mcqs

    pending = [m for m in (mcqs or [])
               if "<img" not in (m.get("explanation", "") or "").lower() and not m.get("exp_bbox")]

    ocr_bboxes = {}
    if pending:
        ocr_bboxes = await asyncio.to_thread(_ocr_bbox_lookup, img, pending)

    full_img_url = None  # lazily uploaded once, reused for every remaining miss
    full_img_attempted = False

    for m in mcqs or []:
        exp = m.get("explanation", "") or ""
        if "<img" in exp.lower():
            continue  # আগে থেকেই attach হয়ে গেছে (Gemini extraction path)

        bbox = m.get("exp_bbox") or ocr_bboxes.get(m.get("question", ""))
        url = ""
        top_pct = bottom_pct = None
        if bbox:
            try:
                result = await asyncio.to_thread(crop_explanation_image, img, bbox)
                url = result.get("url", "")
                top_pct = result.get("top_pct")
                bottom_pct = result.get("bottom_pct")
            except Exception as e:
                logger.warning(f"[ExplanationCrop] wrapper-attach failed: {e}")

        if not url:
            if full_img_url is None and not full_img_attempted:
                for _try in range(2):
                    try:
                        b64 = await asyncio.to_thread(image_to_base64, img)
                        full_img_url = await asyncio.to_thread(upload_to_imgbb, b64) or ""
                        if full_img_url:
                            break
                    except Exception as e:
                        logger.warning(f"[ExplanationCrop] full-image fallback attempt {_try+1} failed: {e}")
                full_img_attempted = True
            url = full_img_url or ""

        if url:
            crop_attrs = ""
            if top_pct is not None and bottom_pct is not None:
                crop_attrs = f' data-crop-top="{top_pct}" data-crop-bottom="{bottom_pct}"'
            m["explanation"] = f'{exp} <img src="{url}"{crop_attrs}>'.strip()

    return mcqs


def _strip_option_prefix(text: str) -> str:
    """Removes leading option labels like 'A.', 'B)', 'ক.', 'খ)', 'a.', etc.
    so the model's own numbering never leaks into the displayed option text."""
    if not text:
        return text
    return re.sub(r'^\s*[A-Da-dক-ঘ]\s*[).।:]\s*', '', str(text)).strip()

def _cap_mcq_options(mcqs: list, max_opts: int = 4) -> list:
    """v4.4: some AI providers occasionally return 5 options (E) instead of 4.
    Trim every mcq down to max_opts here — single choke point so /img's
    Telegram poll, Web Exam page, Quiz Solve, and CSV export all stay
    consistent without needing separate truncation logic in each consumer.
    Also strips any leading option-label prefix (A./ক./a) etc.) the model
    may have echoed into the option text itself — same choke point."""
    if not mcqs:
        return mcqs
    ans_map = {0: "A", 1: "B", 2: "C", 3: "D", 4: "E"}
    rev_map = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
    for m in mcqs:
        opts = m.get("options", [])
        opts = [_strip_option_prefix(o) for o in opts]
        if len(opts) > max_opts:
            ans_letter = m.get("answer", "A")
            ans_idx = rev_map.get(ans_letter, 0)
            # If the correct answer happens to be option 5 (E), keep it in range
            # by swapping it into slot 4 (D) before trimming, so we never lose
            # the right answer off the end.
            if ans_idx >= max_opts:
                opts = opts[:max_opts - 1] + [opts[ans_idx]]
                m["answer"] = ans_map[max_opts - 1]
            else:
                opts = opts[:max_opts]
        m["options"] = opts
    return mcqs


async def _generate_mcq_from_image_raw(img, topic, page_num, mcq_count=None):
    # 1) Groq (primary — fast, set via GROQ_API_KEY)
    try:
        out = await _gen_groq(img, topic, mcq_count)
        if out:
            logger.info(f"[AI-ROT] page {page_num} satisfied by provider=groq")
            return out
        logger.warning(f"[AI-ROT] groq returned empty (page {page_num}); trying gemini")
    except Exception as e:
        logger.warning(f"[AI-ROT] groq failed (page {page_num}): {e}; trying gemini")

    # 2) Gemini (secondary — healthy key → use it)
    try:
        out = await _gemini_gen_mcq(img, topic, page_num, mcq_count)
        if out:
            return out
        logger.warning(f"[AI-ROT] gemini returned empty (page {page_num}); rotating to fallbacks")
    except Exception as e:
        logger.warning(f"[AI-ROT] gemini failed (page {page_num}): {e}; rotating to fallbacks")

    # 3) Fallback providers (skip silently if key missing / call fails)
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


def _strip_img_tag(exp: str) -> str:
    """CSV-তে <img> tag থাকার দরকার নেই (ওটা শুধু webquiz result page-এর জন্য) —
    CSV export-এর সব জায়গায় ব্যবহার করো explanation-কে plain রাখতে।"""
    return re.sub(r'\s*<img\b[^>]*>\s*', ' ', exp or "", flags=re.IGNORECASE).strip()
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
    # BUG FIX: previously defaulted to False. Since /pin on|off is typed in the
    # admin's DM with the bot (setting pin_{dm_chat_id}) while try_pin_message
    # is checked against pin_{channel_id} — a completely different key — the
    # setting could never actually be turned on for the channel that matters.
    # Default to True (auto-pin, the originally intended always-on behavior)
    # so first-image/summary/PDF pinning works out of the box; explicit
    # /pin off (once wired to the right chat_id) still overrides this.
    return True

async def db_set_pin_setting(chat_id, enabled: bool):
    try:
        sb.table("bot_settings").upsert({
            "key": f"pin_{chat_id}",
            "value": "on" if enabled else "off"
        }).execute()
    except Exception as e:
        logger.error(f"[DB] set_pin error: {e}")

async def db_get_pdf_autosend_setting(chat_id) -> bool:
    try:
        r = sb.table("bot_settings").select("value").eq("key", f"pdfauto_{chat_id}").execute()
        if r.data:
            return r.data[0]["value"] == "on"
    except:
        pass
    return True

async def db_set_pdf_autosend_setting(chat_id, enabled: bool):
    try:
        sb.table("bot_settings").upsert({
            "key": f"pdfauto_{chat_id}",
            "value": "on" if enabled else "off"
        }).execute()
    except Exception as e:
        logger.error(f"[DB] set_pdf_autosend error: {e}")

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
    try:
        from core import _ensure_d1_table, d1_run as _d1r
        await _ensure_d1_table("live_quiz_results",
            "CREATE TABLE IF NOT EXISTS live_quiz_results (session_id TEXT, user_id INTEGER, user_name TEXT, "
            "correct INTEGER, wrong INTEGER, skipped INTEGER, total INTEGER, avg_response_time REAL, score INTEGER, "
            "updated_at INTEGER, PRIMARY KEY (session_id, user_id))")
        await _d1r(
            "INSERT INTO live_quiz_results (session_id,user_id,user_name,correct,wrong,skipped,total,avg_response_time,score,updated_at) "
            "VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10) "
            "ON CONFLICT(session_id,user_id) DO UPDATE SET user_name=excluded.user_name, correct=excluded.correct, "
            "wrong=excluded.wrong, skipped=excluded.skipped, total=excluded.total, "
            "avg_response_time=excluded.avg_response_time, score=excluded.score, updated_at=excluded.updated_at",
            [session_id, user_id, user_name, correct, wrong, skipped, total, avg_time, correct, int(time.time())]
        )
    except Exception as e:
        logger.warning(f"[D1] save_live_result mirror warn: {e}")

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
            "• <code>/img</code> — Image reply করে MCQ poll channel-এ (e.g. <code>/img Physics 5</code>)\n"
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
    text = re.sub(r'(?i)^/tagq', '', msg.get("text", "")).strip()
    if text:
        sb.table("quiz_settings").upsert({"id": 1, "tag": text}).execute()
        await db_save_settings_field("tag", text)
        await send_msg(chat_id, f"✅ Tag set:\n{text}")
    else:
        s = await db_get_settings()
        await send_msg(chat_id, f"🔖 Current tag:\n{s.get('tag') or 'None'}\n\nSet: /tagQ [text]")

async def handle_expQ(msg: dict):
    chat_id = msg["chat"]["id"]
    text = re.sub(r'(?i)^/expq', '', msg.get("text", "")).strip()
    if text:
        sb.table("quiz_settings").upsert({"id": 1, "exp_footer": text}).execute()
        await db_save_settings_field("exp_footer", text)
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
        await db_save_channel(channel_id, display)
        await send_msg(chat_id, f"✅ Channel added: {channel_id}\n📛 Name: {display}")
    else:
        await send_msg(chat_id,
            "❌ Invalid!\n\n"
            "<b>Usage:</b>\n"
            "<code>/channel @name</code>\n"
            "<code>/channel -100xxx Custom Name</code>\n"
            "<code>/channelist</code> — list all"
        )

async def _show_channel_list(chat_id, edit_message_id=None):
    channels = await db_get_channels()
    if not channels:
        txt = ("📢 No channels saved!\n\n"
               "Add: <code>/channel @name</code>\n"
               "Add: <code>/channel -100xxx Custom Name</code>")
        if edit_message_id:
            await tg_post("editMessageText", {"chat_id": chat_id, "message_id": edit_message_id, "text": txt, "parse_mode": "HTML"})
        else:
            await send_msg(chat_id, txt)
        return
    txt = "📢 <b>Saved Channels</b>\n\nচ্যানেল সিলেক্ট করলে Edit/Delete অপশন পাবে:"
    buttons = []
    for ch in channels:
        ch_id = ch.get("channel_id", "")
        ch_name = ch.get("channel_name", ch_id)
        buttons.append([{"text": f"📢 {ch_name}", "callback_data": f"chsel_{ch_id}"}])
    reply_markup = {"inline_keyboard": buttons}
    if edit_message_id:
        await tg_post("editMessageText", {"chat_id": chat_id, "message_id": edit_message_id, "text": txt,
                                            "parse_mode": "HTML", "reply_markup": reply_markup})
    else:
        await send_msg(chat_id, txt, reply_markup=reply_markup)

async def _show_channel_actions(chat_id, message_id, channel_id):
    channels = await db_get_channels()
    ch = next((c for c in channels if c.get("channel_id") == channel_id), None)
    if not ch:
        await tg_post("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": "❌ Channel পাওয়া যায়নি, হয়তো delete হয়ে গেছে।"})
        return
    ch_name = ch.get("channel_name", channel_id)
    txt = f"📢 <b>{ch_name}</b>\n🔗 <code>{channel_id}</code>\n\nকী করতে চাও?"
    buttons = [
        [{"text": "✏️ Name Update", "callback_data": f"chren_{channel_id}"}],
        [{"text": "🗑️ Delete", "callback_data": f"chdel_{channel_id}"}],
        [{"text": "⬅️ Back", "callback_data": "chback"}]
    ]
    await tg_post("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": txt,
                                        "parse_mode": "HTML", "reply_markup": {"inline_keyboard": buttons}})

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
    # /pin on|off applies to the CHANNEL (via -c), not the DM chat where this
    # is typed — try_pin_message() checks pin_{channel_id}, so the setting
    # must be stored under that same key or it silently never applies.
    c_match = re.search(r'-c\s+(\S+)', text)
    target_id = c_match.group(1) if c_match else chat_id
    arg = re.sub(r'-c\s+\S+', '', text).replace("/pin", "").strip().lower()
    if arg == "on":
        await db_set_pin_setting(target_id, True)
        PIN_ENABLED[target_id] = True
        await send_msg(chat_id, f"📌 Auto-pin চালু ({target_id})! First image, summary, ও PDF pin হবে।")
    elif arg == "off":
        await db_set_pin_setting(target_id, False)
        PIN_ENABLED[target_id] = False
        await send_msg(chat_id, f"📌 Auto-pin বন্ধ ({target_id})!")
    else:
        current = await db_get_pin_setting(target_id)
        await send_msg(chat_id, f"📌 Pin status ({target_id}): {'✅ ON' if current else '❌ OFF'}\n\nChange: /pin on -c @channel | /pin off -c @channel")

async def try_pin_message(chat_id, message_id: int):
    """Channel-এ message pin করার চেষ্টা করে"""
    enabled = PIN_ENABLED.get(chat_id)
    if enabled is None:
        enabled = await db_get_pin_setting(chat_id)
        PIN_ENABLED[chat_id] = enabled
    if enabled:
        r = await tg_post("pinChatMessage", {
            "chat_id": chat_id,
            "message_id": message_id,
            "disable_notification": True
        })
        if not r or not r.get("ok"):
            err_desc = (r or {}).get("description", "no response")
            logger.warning(f"[Pin] FAILED chat={chat_id} msg={message_id}: {err_desc}")
            try:
                await notify_owner(
                    f"⚠️ Auto-pin failed (chat={chat_id}, msg_id={message_id})\n"
                    f"Reason: {err_desc}\n"
                    f"সম্ভবত bot-কে channel-এ 'Pin Messages' admin permission দেওয়া নেই।"
                )
            except Exception:
                pass
    else:
        logger.info(f"[Pin] Skipped (disabled for chat={chat_id})")

# ============================================================
# FEATURE: /pdf on | /pdf off — ending message er por auto Sheet PDF channel e jabe kina
# ============================================================
async def handle_pdf_autosend_toggle(msg: dict, arg: str):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    if not await db_is_owner_or_admin(uid):
        await send_msg(chat_id, "❌ Admin only!")
        return
    text = msg.get("text", "")
    c_match = re.search(r'-c\s+(\S+)', text)
    target_id = c_match.group(1) if c_match else chat_id
    if arg == "on":
        await db_set_pdf_autosend_setting(target_id, True)
        PDF_AUTO_ENABLED[target_id] = True
        await send_msg(chat_id, f"📄 /pdf auto-PDF চালু ({target_id})! এখন থেকে প্রতিটা page/ending message এর পরে Sheet PDF অটো channel এ যাবে।")
    else:
        await db_set_pdf_autosend_setting(target_id, False)
        PDF_AUTO_ENABLED[target_id] = False
        await send_msg(chat_id, f"📄 /pdf auto-PDF বন্ধ ({target_id})!")

async def should_autosend_pdf(chat_id) -> bool:
    enabled = PDF_AUTO_ENABLED.get(chat_id)
    if enabled is None:
        enabled = await db_get_pdf_autosend_setting(chat_id)
        PDF_AUTO_ENABLED[chat_id] = enabled
    return enabled

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
    # Count can appear anywhere in the command: /img 5, /img 5 Physics, /img Physics 5
    raw = re.sub(r"^/img\s*", "", text, flags=re.IGNORECASE).strip()
    mcq_count = None
    m_count = re.search(r'(?:^|\s)(\d+)(?=\s|$)', raw)
    if m_count:
        mcq_count = int(m_count.group(1))
        raw = (raw[:m_count.start()] + raw[m_count.end():]).strip()
        raw = re.sub(r'\s+', ' ', raw)
    topic = raw or "ATLAS Special MCQ"

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
        "data": json.dumps({"file_id": file_id, "msg_id": reply["message_id"], "topic": topic, "mcq_count": mcq_count}),
        "updated_at": int(time.time())
    }).execute()

    # STEP 0 (NEW): source select — New MCQ (AI-generated, present system)
    # vs Existing MCQ (extract already-existing MCQ from the image, qbm-style).
    kb = {"inline_keyboard": [
        [{"text": "🆕 New MCQ (AI generate করবে)", "callback_data": f"imgsrc_new_{uid}"}],
        [{"text": "📋 Existing MCQ (ছবিতে যা আছে তাই বের করবে)", "callback_data": f"imgsrc_existing_{uid}"}]
    ]}
    await send_msg(chat_id,
        f"📸 Image পাওয়া গেছে!\n📌 Topic: <b>{topic}</b>\n\nMCQ কোথা থেকে আসবে?",
        reply_markup=kb, parse_mode="HTML"
    )

async def handle_img_source(source: str, uid: int, chat_id: int, user: dict):
    """source: 'new' (present AI-generate system) or 'existing' (qbm-style extraction, 3-call: extract+miss-check+verify).

    NEW ORDER: source select -> processing (with CSV auto-send) -> channel select -> mode select -> post.
    Mode selection no longer happens here; it happens after the channel is picked
    (see imgchannel_ callback), right before the poll is actually sent."""
    session_key = f"img_cmd_{uid}"
    row = sb.table("quiz_sessions").select("data").eq("key", session_key).execute()
    if not row.data:
        await send_msg(chat_id, "❌ Session expired!")
        return

    img_data = json.loads(row.data[0]["data"])
    img_data["source"] = source
    sb.table("quiz_sessions").upsert({
        "key": session_key,
        "data": json.dumps(img_data),
        "updated_at": int(time.time())
    }).execute()

    # Straight into processing — no mode prompt here anymore.
    await handle_img_process(uid, chat_id, user)

async def handle_img_process(uid: int, chat_id: int, user: dict):
    """Runs MCQ generation/extraction, auto-sends CSV, then shows channel list.
    Mode (Image/Topic) is asked AFTER channel is chosen, not here."""
    session_key = f"img_cmd_{uid}"
    row = sb.table("quiz_sessions").select("data").eq("key", session_key).execute()
    if not row.data:
        await send_msg(chat_id, "❌ Session expired!")
        return

    img_data = json.loads(row.data[0]["data"])
    file_id = img_data["file_id"]
    topic = img_data.get("topic", "ATLAS Special MCQ")
    source = img_data.get("source", "new")
    mcq_count = img_data.get("mcq_count")

    channels = await db_get_channels()
    if not channels:
        await send_msg(chat_id, "❌ কোনো channel save করা নেই! /channel দিয়ে add করো।")
        return

    # ── MCQ processing ALWAYS runs here now (before channel select), same
    # pattern as /qbm: generate/extract first -> CSV auto-sent -> THEN show
    # channel list, so the person picks a channel already knowing the count. ──
    est_secs = 30 if source == "new" else 38
    label = "MCQ তৈরি হচ্ছে" if source == "new" else "Existing MCQ বের করা হচ্ছে"
    loading = await send_msg(chat_id, f"⏳ Image থেকে {label}... 0%")
    loading_id = loading.get("result", {}).get("message_id")

    _progress_stop = asyncio.Event()
    _img_progress = {"done": 0, "total": max(mcq_count or 10, 1)}

    async def _progress_ticker():
        bars = ["▱▱▱▱▱▱▱▱▱▱","▰▱▱▱▱▱▱▱▱▱","▰▰▱▱▱▱▱▱▱▱","▰▰▰▱▱▱▱▱▱▱","▰▰▰▰▱▱▱▱▱▱",
                "▰▰▰▰▰▱▱▱▱▱","▰▰▰▰▰▰▱▱▱▱","▰▰▰▰▰▰▰▱▱▱","▰▰▰▰▰▰▰▰▱▱","▰▰▰▰▰▰▰▰▰▱"]
        smooth_pct = 0.0
        try:
            while not _progress_stop.is_set():
                await asyncio.sleep(0.5)
                if _progress_stop.is_set():
                    break
                real_pct = (_img_progress["done"] / _img_progress["total"]) * 100
                target = min(max(real_pct, smooth_pct + 1.5), 95)
                smooth_pct = min(smooth_pct + max((target - smooth_pct) * 0.25, 0.8), 95)
                pct = int(smooth_pct)
                bar_idx = min(int(pct / 10), len(bars) - 1)
                try:
                    await edit_msg(chat_id, loading_id,
                        f"⏳ Image থেকে {label}...\n"
                        f"📊 Progress: {bars[bar_idx]} {pct}%")
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    ticker_task = asyncio.create_task(_progress_ticker())

    try:
        img_bytes = await download_tg_file(file_id)
        from PIL import Image as PILImage
        img = PILImage.open(BytesIO(img_bytes))

        if source == "existing":
            # Existing MCQ mode: /qbm prompt logic, full 3-call connected pipeline
            # (Call 1 extract + Call 2 miss-check + Call 3 verify) — never fabricates
            # new questions, only extracts what's already in the image, per /qbm rules.
            mcqs = await _qbm_extract_from_image(img)
            mcqs = _cap_mcq_options(_imgqbm_options_to_list(mcqs))
        else:
            mcqs = await generate_mcq_from_image(img, topic, 1, mcq_count)
    except Exception as e:
        _progress_stop.set()
        ticker_task.cancel()
        logger.error(f"[IMG] Processing error: {e}", exc_info=True)
        await _safe_error_reply(chat_id, e)
        return

    _progress_stop.set()
    ticker_task.cancel()

    if not mcqs:
        msg = "❌ MCQ generate হয়নি!" if source == "new" else "❌ ছবিতে কোনো existing MCQ পাওয়া যায়নি!"
        await send_msg(chat_id, msg)
        return

    # ✅ CSV auto-send — processing শেষ হওয়া মাত্রই, channel select করার আগেই
    try:
        import csv as _csv
        from io import StringIO as _SIO
        _out = _SIO()
        _wr = _csv.writer(_out, quoting=_csv.QUOTE_ALL)
        _wr.writerow(["questions", "option1", "option2", "option3", "option4", "option5", "answer", "explanation", "type", "section"])
        for m in mcqs:
            opts = m.get("options", ["", "", "", ""])
            padded = (list(opts) + ["", "", "", "", ""])[:5]
            ans_idx = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}.get(m.get("answer", "A"), 0)
            ans_numeric = ans_idx + 1  # 1-based
            exp = _strip_img_tag(m.get("explanation", ""))
            _wr.writerow([m.get("question", ""), padded[0], padded[1], padded[2], padded[3], padded[4], ans_numeric, exp, 1, 1])
        csv_content = _out.getvalue().encode("utf-8-sig")
        csv_caption = (
            f"📄 CSV ফাইল — {topic}\n"
            f"💎 {len(mcqs)} MCQ\n\n"
            f"📌 Format: questions, option1-5, answer(numeric), explanation, type, section"
        )
        try:
            from quiz import create_quiz_from_mcqs
            bot_quiz_id = await create_quiz_from_mcqs(mcqs, topic or "ATLAS MCQ", uid)
            bot_info = await tg_post("getMe", {})
            bot_un = bot_info.get("result", {}).get("username", "")

            from poll_extract import save_quiz_to_d1
            polls = [{"question": m["question"], "options": m.get("options", ["", "", "", ""]),
                       "correct_idx": {"A": 0, "B": 1, "C": 2, "D": 3}.get(m.get("answer", "A"), 0),
                       "explanation": m.get("explanation", "")}
                      for m in mcqs]
            web_quiz_id = await save_quiz_to_d1(polls, topic or "ATLAS MCQ", uid)
            web_url = f"https://hamza818483-dotcom.github.io/QuizBot/exam.html?id={web_quiz_id}"

            csv_caption += (
                f"\n\n🌐 Web Quiz: {web_url}"
                f"\n🤖 Bot Quiz: https://t.me/{bot_un}?start={bot_quiz_id}"
            )
        except Exception as link_err:
            logger.warning(f"[IMG] Quiz link generation failed: {link_err}")
        await send_document(
            chat_id, csv_content,
            f"ATLAS_{topic or 'MCQ'}.csv",
            caption=csv_caption, mime_type="text/csv"
        )
    except Exception as csv_err:
        logger.warning(f"[IMG] CSV auto-send failed: {csv_err}")

    # Cache the already-processed mcqs + raw image bytes so channel-select
    # posts directly without re-running generation/extraction. In-memory cache
    # is the fast path; mcqs are ALSO persisted in the DB session below so a
    # server restart between steps never forces a redo of the AI call —
    # only img_bytes would need a cheap re-download from Telegram (no AI cost).
    app.state.img_cache = getattr(app.state, "img_cache", {})
    app.state.img_cache[f"img_mcq_{uid}"] = {"mcqs": mcqs, "img_bytes": img_bytes}

    sb.table("quiz_sessions").upsert({
        "key": f"img_mode_{uid}",
        "data": json.dumps({"file_id": file_id, "topic": topic, "source": source, "mcq_count": mcq_count, "mcqs": mcqs}),
        "updated_at": int(time.time())
    }).execute()

    await edit_msg(chat_id, loading_id, f"✅ Processing Complete! {len(mcqs)} MCQ পাওয়া গেছে")

    kb = {"inline_keyboard": []}
    for ch in channels:
        ch_id = ch.get("channel_id", "")
        ch_name = ch.get("channel_name", ch_id)
        kb["inline_keyboard"].append([{
            "text": f"📢 {ch_name}",
            "callback_data": f"imgchannel_{ch_id}_{uid}"
        }])
    await send_msg(chat_id,
        f"📌 Topic: <b>{topic}</b>\n\nকোন channel-এ পাঠাবে?",
        reply_markup=kb, parse_mode="HTML")

def _imgqbm_options_to_list(mcqs: list) -> list:
    """/qbm extraction returns options as a dict {A,B,C,D}; /img's poll-sender
    (and _cap_mcq_options) expect options as a list. Convert format only —
    never touch question text/answer/explanation content or order."""
    out = []
    for m in mcqs:
        opts = m.get("options")
        if isinstance(opts, dict):
            opts = [opts.get("A", ""), opts.get("B", ""), opts.get("C", ""), opts.get("D", "")]
        m2 = dict(m)
        m2["options"] = opts or []
        out.append(m2)
    return out


async def process_img_to_poll(file_id: str, channel_id: str, mode: str,
                               chat_id: int, uid: int, uname: str, topic: str = "ATLAS Special MCQ",
                               source: str = "new", mcq_count=None):
    settings = await db_get_settings()
    tag = settings.get("tag", "")
    exp_footer = settings.get("exp_footer", "")

    # Reuse already-processed MCQs + image bytes from handle_img_process (Phase 1)
    # so channel select never re-triggers generation/extraction or a 2nd CSV.
    cache = getattr(app.state, "img_cache", {}).get(f"img_mcq_{uid}")
    loading_id = None
    if cache:
        mcqs = cache["mcqs"]
        img_bytes = cache["img_bytes"]
    else:
        # In-memory cache missing (e.g. server restarted between steps) —
        # check DB-persisted mcqs first (saved in handle_img_process) so we
        # NEVER re-run the AI generation/extraction a 2nd time. Only the raw
        # image bytes need a cheap re-download from Telegram (no AI cost).
        db_mcqs = None
        try:
            row = sb.table("quiz_sessions").select("data").eq("key", f"img_mode_{uid}").execute()
            if row.data:
                saved = json.loads(row.data[0]["data"])
                db_mcqs = saved.get("mcqs")
        except Exception as e:
            logger.warning(f"[IMG] DB mcqs lookup failed: {e}")

        if db_mcqs:
            mcqs = db_mcqs
            try:
                img_bytes = await download_tg_file(file_id)
            except Exception as e:
                logger.error(f"[IMG] Image re-download error: {e}", exc_info=True)
                await _safe_error_reply(chat_id, e)
                return
        else:
            # Truly nothing saved anywhere (very first run edge-case) -> full re-processing.
            loading_text = "⏳ Image থেকে MCQ তৈরি হচ্ছে... (~30s)" if source == "new" else "⏳ Image থেকে existing MCQ বের করা হচ্ছে... (~30-40s)"
            loading = await send_msg(chat_id, loading_text)
            loading_id = loading.get("result", {}).get("message_id")
            try:
                img_bytes = await download_tg_file(file_id)
                from PIL import Image as PILImage
                img = PILImage.open(BytesIO(img_bytes))
                if source == "existing":
                    mcqs = await _qbm_extract_from_image(img)
                    mcqs = _cap_mcq_options(_imgqbm_options_to_list(mcqs))
                else:
                    mcqs = await generate_mcq_from_image(img, topic, 1, mcq_count)
            except Exception as e:
                logger.error(f"[IMG] Re-processing error: {e}", exc_info=True)
                await _safe_error_reply(chat_id, e)
                return

    if not mcqs:
        msg = "❌ MCQ generate হয়নি!" if source == "new" else "❌ ছবিতে কোনো existing MCQ পাওয়া যায়নি!"
        await send_msg(chat_id, msg)
        return

    try:
        image_msg_id = None
        image_file_id = None  # bot-owned, reusable file_id (matches /pdf pattern)

        caption = ""
        if tag:
            caption = f"{tag}\n\n"
        caption += (
            f"⌛ATLAS Special MCQ System\n"
            f"🌟Topic: {topic}\n"
            f"💎MCQ: {len(mcqs)}"
        )
        photo_r = await send_photo(channel_id, img_bytes, caption)
        if photo_r.get("ok"):
            image_msg_id = photo_r["result"]["message_id"]
            image_file_id = photo_r["result"]["photo"][-1]["file_id"]

        if mode != "image" and image_msg_id:
            # Topic Mode: photo শুধু fresh file_id নেওয়ার জন্য পাঠানো হলো, channel-এ দেখানো হবে না।
            # কিন্তু polls/end message-এর reply করার জন্য কিছু একটা লাগবে, তাই photo delete করে
            # তার জায়গায় একটা text pre-message পাঠানো হচ্ছে (RononBot-এর Without-Image
            # pattern-এর মতো) — সেটাকেই এখন থেকে reply_to_message_id হিসেবে ব্যবহার করা হবে।
            await tg_post("deleteMessage", {"chat_id": channel_id, "message_id": image_msg_id})
            pre_text = ""
            if tag:
                pre_text = f"{tag}\n\n"
            pre_text += (
                f"⌛ATLAS Special MCQ System\n"
                f"🌟Topic: {topic}\n"
                f"💎MCQ: {len(mcqs)}"
            )
            pre_r = await tg_post("sendMessage", {"chat_id": channel_id, "text": pre_text})
            image_msg_id = pre_r.get("result", {}).get("message_id") if pre_r.get("ok") else None

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
            poll_r = {"ok": False}
            for _attempt in range(3):
                poll_r = await send_poll(
                    channel_id, q_text, opts, ans_idx,
                    explanation=exp,
                    reply_to_message_id=image_msg_id
                )
                if poll_r.get("ok"):
                    break
                logger.warning(f"[ImgPoll] q{i+1}/{len(mcqs)} attempt {_attempt+1} failed, retrying...")
                await asyncio.sleep(2)
            if not poll_r.get("ok"):
                logger.error(f"[ImgPoll] sendPoll FINAL FAIL q{i+1}/{len(mcqs)} opts={len(opts)}: {poll_r.get('description') or poll_r.get('error')}")
            if poll_r.get("ok") and i == 0:
                msg_id = poll_r["result"]["message_id"]
                cid = str(channel_id)
                if cid.startswith("-100"):
                    poll_links.append(f"https://t.me/c/{cid[4:]}/{msg_id}")
                else:
                    poll_links.append(f"https://t.me/{cid.lstrip('@')}/{msg_id}")
            await asyncio.sleep(0.5)

        # CSV already auto-sent in handle_img_process right after processing —
        # not repeated here to avoid sending it twice.

        end_text = (
            f"🚀Topic: {topic}\n"
            f"🌟Page No: N/A\n"
            f"✅MCQ: {len(mcqs)}\n"
        )
        if poll_links:
            end_text += f"🔗First Poll Link:\n{poll_links[0]}"

        # ✅ নতুন: cache save করো যাতে buttons কাজ করে
        cache_id_img = gen_session_id()
        await db_save_mcq_cache(cache_id_img, cache_id_img, 1, topic, mcqs, poll_links,
                                image_file_id, image_msg_id, channel_id)

        exam_url = f"{GH_PAGES_EXAM_URL}?id={cache_id_img}"
        bot_un = await get_bot_username()
        quiz_url = f"https://t.me/{bot_un}?start=pdf_{cache_id_img}"
        poll_url = f"https://t.me/{bot_un}?start=poll_{cache_id_img}"

        end_kb = {"inline_keyboard": [
            [{"text": "📝 Quiz Solve", "url": quiz_url},
             {"text": "🔄 Poll Solve", "url": poll_url}],
            [{"text": "🌐 Web Exam", "url": exam_url},
             {"text": "💎 Premium PDF", "url": f"https://t.me/{bot_un}?start=premium_{cache_id_img}"}]
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
        else:
            await send_msg(chat_id, f"✅ Done! {len(mcqs)} MCQ পাঠানো হয়েছে channel-এ।")

    except Exception as e:
        logger.error(f"[IMG] Error: {e}")
        await _safe_error_reply(chat_id, e)

# ============================================================
# FEATURE: /txt — Text reply → Poll
# ============================================================
async def handle_txt_command(msg: dict):
    """
    Text message-এ reply করে /txt দিলে সাথে সাথে MCQ generate + CSV তৈরি হয়ে যাবে,
    এরপর channel select করার অপশন আসবে (poll পাঠানোর জন্য)।
    """
    import io, csv as csv_mod

    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    reply = msg.get("reply_to_message")

    if not reply or not reply.get("text"):
        await send_msg(chat_id, "❌ কোনো text message-এ reply করে /txt দাও!")
        return

    text_content = reply["text"]

    loading = await send_msg(chat_id, "🔄 Text থেকে MCQ তৈরি হচ্ছে...\n⏱️ আনুমানিক সময়: 7 সেকেন্ড\n📊 Progress: ▱▱▱▱▱▱▱ 0%\n✅ তৈরি হয়েছে: 0 টি MCQ")
    loading_id = loading.get("result", {}).get("message_id")

    try:
        from pdf_handler import generate_mcq_from_text
        line_count = len([l for l in text_content.splitlines() if l.strip()])
        auto_count = line_count  # soft hint only; prompt enforces quality-driven count

        progress = {"done": 0, "total": max(auto_count, 1)}

        def _on_mcq_done(n: int):
            progress["done"] = n

        async def _progress_updater():
            bars = ["▱▱▱▱▱▱▱▱▱▱","▰▱▱▱▱▱▱▱▱▱","▰▰▱▱▱▱▱▱▱▱","▰▰▰▱▱▱▱▱▱▱","▰▰▰▰▱▱▱▱▱▱",
                    "▰▰▰▰▰▱▱▱▱▱","▰▰▰▰▰▰▱▱▱▱","▰▰▰▰▰▰▰▱▱▱","▰▰▰▰▰▰▰▰▱▱","▰▰▰▰▰▰▰▰▰▱"]
            smooth_pct = 0.0
            while True:
                await asyncio.sleep(0.4)
                real_pct = (progress["done"] / progress["total"]) * 100
                target = min(max(real_pct, smooth_pct + 2), 95)
                smooth_pct = min(smooth_pct + max((target - smooth_pct) * 0.3, 1.0), 95)
                pct = int(smooth_pct)
                bar_idx = min(int(pct / 10), len(bars) - 1)
                if loading_id:
                    try:
                        await edit_msg(chat_id, loading_id,
                            f"🔄 Text থেকে MCQ তৈরি হচ্ছে...\n⏱️ আনুমানিক সময়: 7 সেকেন্ড\n"
                            f"📊 Progress: {bars[bar_idx]} {pct}%\n✅ তৈরি হয়েছে: {progress['done']} টি MCQ")
                    except Exception:
                        pass

        progress_task = asyncio.create_task(_progress_updater())
        try:
            mcqs = await generate_mcq_from_text(text_content, "ATLAS MCQ", count=auto_count,
                                                 on_progress=_on_mcq_done)
        except TypeError:
            # pdf_handler.generate_mcq_from_text doesn't support on_progress yet
            mcqs = await generate_mcq_from_text(text_content, "ATLAS MCQ", count=auto_count)
        progress_task.cancel()

        if not mcqs:
            if loading_id:
                await edit_msg(chat_id, loading_id, "❌ MCQ generate হয়নি!")
            return

        mcqs = _cap_mcq_options(mcqs, 4)

        if loading_id:
            await edit_msg(chat_id, loading_id,
                f"✅ তৈরি হয়েছে: {len(mcqs)} টি MCQ\n📊 Progress: ▰▰▰▰▰▰▰ 100%")

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
                             ans_num, _strip_img_tag(m.get("explanation","")), "1", "1"])
        csv_bytes = buf.getvalue().encode("utf-8")

        caption = f"📄 {len(mcqs)} MCQ CSV"
        try:
            from quiz import create_quiz_from_mcqs
            bot_quiz_id = await create_quiz_from_mcqs(mcqs, "ATLAS MCQ", uid)
            bot_info = await tg_post("getMe", {})
            bot_un = bot_info.get("result", {}).get("username", "")

            from poll_extract import save_quiz_to_d1
            polls = [{"question": m["question"], "options": m.get("options", ["","","",""]),
                       "correct_idx": {"A":0,"B":1,"C":2,"D":3}.get(m.get("answer","A"), 0),
                       "explanation": m.get("explanation","")}
                      for m in mcqs]
            web_quiz_id = await save_quiz_to_d1(polls, "ATLAS MCQ", uid)
            web_url = f"https://hamza818483-dotcom.github.io/QuizBot/exam.html?id={web_quiz_id}"

            caption += (
                f"\n\n🌐 Web Quiz: {web_url}"
                f"\n🤖 Bot Quiz: https://t.me/{bot_un}?start={bot_quiz_id}"
            )

        except Exception as e:
            logger.error(f"[TXT] link gen error: {e}")

        await send_document(chat_id, csv_bytes,
            "ATLAS_mcq.csv", caption=caption, mime_type="text/csv")

        # MCQ result cache করে রাখা হচ্ছে channel select করার সময় ব্যবহারের জন্য
        sb.table("quiz_sessions").upsert({
            "key": f"txt_cmd_{uid}",
            "data": json.dumps({"mcqs": mcqs}),
            "updated_at": int(time.time())
        }).execute()

        channels = await db_get_channels()
        if not channels:
            await send_msg(chat_id, "✅ CSV তৈরি হয়ে গেছে। কোনো channel নেই তাই poll পাঠানো যাবে না। /channel দিয়ে add করো।")
            return

        kb = {"inline_keyboard": []}
        for ch in channels:
            ch_id = ch.get("channel_id", "")
            ch_name = ch.get("channel_name", ch_id)
            kb["inline_keyboard"].append([{
                "text": f"📢 {ch_name}",
                "callback_data": f"txtchannel_{ch_id}_{uid}"
            }])
        await send_msg(chat_id,
            f"✅ {len(mcqs)} টি MCQ তৈরি ও CSV পাঠানো হয়েছে!\nPoll পাঠাতে channel select করো:",
            reply_markup=kb
        )

    except Exception as e:
        logger.error(f"[TXT] Error: {e}")
        await _safe_error_reply(chat_id, e)

async def process_txt_to_poll(channel_id: str, chat_id: int, uid: int, uname: str):
    _active_jobs["count"] = _active_jobs.get("count", 0) + 1
    try:
        return await _process_txt_to_poll_inner(channel_id, chat_id, uid, uname)
    finally:
        _active_jobs["count"] = max(0, _active_jobs.get("count", 1) - 1)

async def _process_txt_to_poll_inner(channel_id: str, chat_id: int, uid: int, uname: str):
    """Cache করা MCQ থেকে সরাসরি poll পাঠাও (আবার generate করবে না)"""
    row = sb.table("quiz_sessions").select("data").eq("key", f"txt_cmd_{uid}").execute()
    if not row.data:
        await send_msg(chat_id, "❌ Session expired!")
        return
    txt_data = json.loads(row.data[0]["data"])
    mcqs = txt_data.get("mcqs", [])
    if not mcqs:
        await send_msg(chat_id, "❌ MCQ data পাওয়া যায়নি!")
        return

    settings = await db_get_settings()
    tag = settings.get("tag", "")
    exp_footer = settings.get("exp_footer", "")

    status_msg = await send_msg(chat_id, f"📤 {len(mcqs)} টি poll পাঠানো হচ্ছে...")
    status_id = status_msg.get("result", {}).get("message_id")

    sent, failed = 0, 0
    for i, mcq in enumerate(mcqs):
        opts = [o[:100] for o in mcq.get("options", [])[:4]]
        ans_idx = {"A":0,"B":1,"C":2,"D":3}.get(mcq.get("answer","A"), 0)
        q_text = mcq["question"][:295]
        if tag:
            q_text = f"{tag}\n\n{q_text}"
        q_text = q_text[:300]
        exp = mcq.get("explanation","")
        if exp_footer:
            exp = f"{exp}\n{exp_footer}"
        poll_r = await send_poll(channel_id, q_text, opts, ans_idx, explanation=exp)
        if poll_r and poll_r.get("ok"):
            sent += 1
        else:
            failed += 1
            logger.error(f"[TXT] sendPoll failed q{i+1}: {poll_r.get('description') if poll_r else 'no response'}")
        await asyncio.sleep(1.0)

    status = f"✅ {sent} MCQ poll পাঠানো হয়েছে!"
    if failed:
        status += f"\n⚠️ {failed}টা পাঠাতে ব্যর্থ (channel এ bot admin আছে কিনা চেক করো)।"
    if status_id:
        await edit_msg(chat_id, status_id, status)
    else:
        await send_msg(chat_id, status)

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
        await _safe_error_reply(chat_id, e)

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
        await _safe_error_reply(chat_id, e)

# ============================================================
# SHARED CSV PARSER
# ============================================================
def _parse_csv_bytes(csv_bytes: bytes) -> list:
    """CSV bytes থেকে MCQ list বানাও."""
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
            ans_map = {"1": "A", "2": "B", "3": "C", "4": "D", "A": "A", "B": "B", "C": "C", "D": "D"}
            ans = ans_map.get(ans_raw, "A")
            mcqs.append({
                "question": _strip_q_numbering(q.strip()), "options": opts, "answer": ans,
                "explanation": row.get("explanation", "").strip()
            })
        return mcqs
    except Exception as e:
        logger.error(f"[CSV Parse] Error: {e}")
        return []

def _mcqs_to_csv_bytes(mcqs: list) -> bytes:
    """MCQ list → CSV bytes, matching _parse_csv_bytes column layout."""
    import io, csv as csv_mod_local
    buf = io.StringIO()
    w = csv_mod_local.writer(buf)
    w.writerow(["questions", "option1", "option2", "option3", "option4", "answer", "explanation"])
    ans_map = {"A": "1", "B": "2", "C": "3", "D": "4"}
    for m in mcqs:
        opts = (m.get("options", []) + ["", "", "", ""])[:4]
        w.writerow([
            m.get("question", ""), opts[0], opts[1], opts[2], opts[3],
            ans_map.get(m.get("answer", "A"), "1"), _strip_img_tag(m.get("explanation", ""))
        ])
    return buf.getvalue().encode("utf-8-sig")

async def handle_split_command(msg: dict):
    _active_jobs["count"] = _active_jobs.get("count", 0) + 1
    try:
        return await _handle_split_command_inner(msg)
    finally:
        _active_jobs["count"] = max(0, _active_jobs.get("count", 1) - 1)

async def _handle_split_command_inner(msg: dict):
    """/split <chunk_size> — reply to a CSV file, splits it into multiple
    smaller CSV files of chunk_size MCQs each."""
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    reply = msg.get("reply_to_message")
    if not reply or not reply.get("document"):
        await send_msg(chat_id, "❌ CSV ফাইলে reply করে <code>/split 20</code> দাও")
        return
    parts = text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await send_msg(chat_id, "❌ সংখ্যা দাও! যেমন: <code>/split 20</code>")
        return
    chunk_size = int(parts[1])
    if chunk_size < 1:
        await send_msg(chat_id, "❌ সংখ্যা ১ বা তার বেশি হতে হবে!")
        return

    status_r = await send_msg(chat_id, "⏳ ফাইল ডাউনলোড হচ্ছে...")
    status_msg_id = status_r.get("result", {}).get("message_id")

    file_id = reply["document"]["file_id"]
    file_name = reply["document"].get("file_name", "file.csv")
    try:
        csv_bytes = await download_tg_file(file_id)
        mcqs = _parse_csv_bytes(csv_bytes)
    except Exception as e:
        logger.error(f"[Split] error: {e}", exc_info=True)
        if status_msg_id:
            await edit_msg(chat_id, status_msg_id, f"❌ Error: {e}")
        return
    if not mcqs:
        if status_msg_id:
            await edit_msg(chat_id, status_msg_id, "❌ ফাইলে কোনো MCQ পাওয়া যায়নি!")
        return

    total = len(mcqs)
    total_parts = (total + chunk_size - 1) // chunk_size
    if status_msg_id:
        await edit_msg(chat_id, status_msg_id, f"⏳ {total}টি MCQ → {total_parts}টি ফাইলে ভাগ হচ্ছে...")

    base_name = re.sub(r'\.(csv|json)$', '', file_name, flags=re.I)
    try:
        for i in range(total_parts):
            chunk = mcqs[i * chunk_size:(i + 1) * chunk_size]
            part_bytes = _mcqs_to_csv_bytes(chunk)
            part_name = f"{base_name}_part{i+1:02d}.csv"
            r = await send_document(chat_id, part_bytes, part_name,
                caption=f"📄 Part-{i+1:02d} | 📊 {len(chunk)}টি MCQ")
            if not r.get("ok"):
                logger.error(f"[Split] send_document failed: {r}")
            await asyncio.sleep(0.5)
    except Exception as e:
        logger.error(f"[Split] send loop error: {e}", exc_info=True)
        if status_msg_id:
            await edit_msg(chat_id, status_msg_id, f"❌ Error: {e}")
        return

    if status_msg_id:
        await edit_msg(chat_id, status_msg_id, f"✅ সম্পন্ন! {total}টি MCQ → {total_parts}টি ফাইল")


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
        opts = mcq.get("options", [])[:4]
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
                explanation=exp,
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
            exam_url = f"{GH_PAGES_EXAM_URL}?id={batch_cache_id}"
            bot_un = await get_bot_username()
            quiz_url = f"https://t.me/{bot_un}?start=pdf_{batch_cache_id}"
            poll_url = f"https://t.me/{bot_un}?start=poll_{batch_cache_id}"
            premium_url = f"https://t.me/{bot_un}?start=premium_{batch_cache_id}"
            end_kb = {"inline_keyboard": [
                [{"text": "📝 Quiz Solve", "url": quiz_url},
                 {"text": "🔄 Poll Again", "url": poll_url}],
                [{"text": "🌐 Website Exam", "url": exam_url},
                 {"text": "💎 Premium PDF", "url": premium_url}],
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
        exam_url = f"{GH_PAGES_EXAM_URL}?id={cache_id}"
        bot_un = await get_bot_username()
        quiz_url = f"https://t.me/{bot_un}?start=pdf_{cache_id}"
        poll_url = f"https://t.me/{bot_un}?start=poll_{cache_id}"
        premium_url = f"https://t.me/{bot_un}?start=premium_{cache_id}"
        end_kb = {"inline_keyboard": [
            [{"text": "📝 Quiz Solve", "url": quiz_url},
             {"text": "🔄 Poll Again", "url": poll_url}],
            [{"text": "🌐 Website Exam", "url": exam_url},
             {"text": "💎 Premium PDF", "url": premium_url}],
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
    """Premium PDF button clicked — generate Style1 PDF from cache with live progress"""
    chat_id = msg["chat"]["id"]
    r = await send_msg(chat_id, "🎨 Premium PDF (Style1)\n⏳ 0% — শুরু হচ্ছে...")
    status_id = r.get("result", {}).get("message_id")
    start_t = time.time()

    async def _progress(pct):
        if not status_id:
            return
        elapsed = time.time() - start_t
        try:
            await edit_msg(chat_id, status_id, f"🎨 Premium PDF (Style1)\n⏳ {pct}% — {elapsed:.1f}s")
        except Exception:
            pass

    try:
        cache = await db_get_mcq_cache(cache_id)
        if not cache:
            await send_msg(chat_id, "❌ Cache পাওয়া যায়নি!")
            return
        topic = cache.get("topic", "MCQ")
        mcqs = cache["mcq_data"]
        await _progress(10)
        data_adapted = _adapt_mcqs_for_print(mcqs)
        html = PRINT_STYLE_BUILDERS["style1"](data_adapted, topic)
        pdf_bytes = await _html_to_pdf(html, progress_cb=_progress)
        pdf_bytes = await _apply_saved_watermark(pdf_bytes)
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
    wm_text = re.sub(r"^/(?:watermark|wm)\s*", "", text, flags=re.IGNORECASE).strip()
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
            asyncio.create_task(_apply_watermark_to_pdf(chat_id, file_id, wm_text, reply["message_id"]))
            return

    await send_msg(chat_id,
        f"✅ Default watermark set: <b>{wm_text}</b>\n\n"
        f"এখন থেকে সব PDF এ এই watermark apply হবে।\n"
        f"যেকোনো পুরনো PDF এ reply করে <code>/wm {wm_text}</code> দিলে সেটায় apply হবে।",
        parse_mode="HTML"
    )


async def _apply_watermark_to_pdf(chat_id: int, file_id: str, wm_text: str, message_id: int = None):
    """Download PDF, apply watermark using existing add_watermark_to_pdf, resend"""
    try:
        pdf_bytes = await download_tg_file(file_id, chat_id=chat_id, message_id=message_id)
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
        await _safe_error_reply(chat_id, e)

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
        pdf_bytes = await _apply_saved_watermark(pdf_bytes)
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
        await _safe_error_reply(chat_id, e)

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
        await _safe_error_reply(chat_id, e)


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

        exam_url = f"{GH_PAGES_EXAM_URL}?id={cache_id}"
        bot_un = await get_bot_username()
        quiz_url = f"https://t.me/{bot_un}?start=pdf_{cache_id}"
        poll_url = f"https://t.me/{bot_un}?start=poll_{cache_id}"
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
        await _safe_error_reply(chat_id, e)

# ============================================================
# HTML → PDF (Chromium)
# ============================================================
_PDF_SEMAPHORE = asyncio.Semaphore(8)

_last_pdf_error = {"msg": ""}

_PW_BROWSER = {"browser": None, "playwright": None}
_PW_LOCK = asyncio.Lock()

async def _get_pw_browser():
    """Reuses a single Playwright browser instance across all PDF generations —
    matches AtlasMasterBot's AsyncPDFExporter pattern (proven working system)."""
    async with _PW_LOCK:
        browser = _PW_BROWSER["browser"]
        if browser is not None and not browser.is_connected():
            logger.warning("[PDF Gen] Cached Playwright browser disconnected — relaunching")
            try:
                await _PW_BROWSER["playwright"].stop()
            except Exception:
                pass
            _PW_BROWSER["browser"] = None
            _PW_BROWSER["playwright"] = None
        if _PW_BROWSER["browser"] is None:
            from playwright.async_api import async_playwright
            _PW_BROWSER["playwright"] = await async_playwright().start()
            _PW_BROWSER["browser"] = await _PW_BROWSER["playwright"].chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"]
            )
        return _PW_BROWSER["browser"]

async def _apply_saved_watermark(pdf_bytes: bytes) -> bytes:
    """If a default watermark is saved (via /wm or /watermark, no reply),
    stamp it onto any generated PDF before sending. Best-effort — returns
    original bytes untouched on any failure or if no watermark is set."""
    if not pdf_bytes:
        return pdf_bytes
    try:
        settings = await db_get_settings()
        wm_text = settings.get("watermark", "")
        if wm_text:
            return add_watermark_to_pdf(pdf_bytes, wm_text)
    except Exception as e:
        logger.warning(f"[AutoWatermark] apply failed: {e}")
    return pdf_bytes

async def _html_to_pdf(html: str, progress_cb=None) -> bytes:
    """Playwright-based HTML->PDF, ported 1:1 from AtlasMasterBot's
    AsyncPDFExporter.html_to_pdf (proven working in production there)."""
    async with _PDF_SEMAPHORE:
        import tempfile
        temp_path = None
        output_path = None
        page = None
        try:
            if progress_cb:
                await progress_cb(15)
            page = None
            last_err = None
            for attempt in range(3):
                try:
                    browser = await _get_pw_browser()
                    page = await browser.new_page()
                    break
                except Exception as e:
                    last_err = e
                    logger.warning(f"[PDF Gen] new_page failed (attempt {attempt+1}/3): {e}, forcing browser relaunch")
                    async with _PW_LOCK:
                        try:
                            if _PW_BROWSER["playwright"]:
                                await _PW_BROWSER["playwright"].stop()
                        except Exception:
                            pass
                        _PW_BROWSER["browser"] = None
                        _PW_BROWSER["playwright"] = None
                    await asyncio.sleep(0.5 * (attempt + 1))
            if page is None:
                raise last_err or Exception("Failed to open browser page after retries")
            if progress_cb:
                await progress_cb(30)

            with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
                f.write(html)
                temp_path = f.name

            await page.goto(f"file://{os.path.abspath(temp_path)}", wait_until="networkidle")
            await page.evaluate("document.fonts.ready")
            await asyncio.sleep(1.5)
            if progress_cb:
                await progress_cb(70)

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as pf:
                output_path = pf.name

            await page.pdf(
                path=output_path, format="A4",
                margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"},
                print_background=True
            )
            if progress_cb:
                await progress_cb(95)

            with open(output_path, "rb") as f:
                pdf_bytes = f.read()
            if progress_cb:
                await progress_cb(100)
            return pdf_bytes
        except Exception as e:
            logger.error(f"[PDF Gen] Playwright error: {e}")
            _last_pdf_error["msg"] = str(e)
            return None
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            for p in (temp_path, output_path):
                if p and os.path.exists(p):
                    try:
                        os.unlink(p)
                    except Exception:
                        pass

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
        pdf_bytes = await _apply_saved_watermark(pdf_bytes)

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
        await _safe_error_reply(chat_id, e)

# ============================================================
# AUTO MHTML/HTML → CSV — file পাঠালেই সাথে সাথে CSV (কোনো command লাগে না)
# ============================================================
async def handle_qcsv_auto(msg: dict):
    chat_id = msg["chat"]["id"]
    doc = msg["document"]
    file_name = doc.get("file_name", "")

    loading = await send_msg(chat_id, "⏳ File পড়া হচ্ছে...")
    loading_id = loading.get("result", {}).get("message_id")

    try:
        raw_bytes = await download_tg_file(doc["file_id"])
        parsed = await asyncio.to_thread(parse_mhtml_to_mcqs, raw_bytes, file_name)
        results = parsed["results"]
        source = parsed["source"] or "Unknown"

        if not results:
            if loading_id:
                await edit_msg(chat_id, loading_id, "❌ কোনো প্রশ্ন/উত্তর খুঁজে পাওয়া যায়নি! Format ভিন্ন হতে পারে।")
            return

        if loading_id:
            await edit_msg(chat_id, loading_id,
                f"✅ {len(results)} টি MCQ পাওয়া গেছে! ({source})\n📄 CSV বানানো হচ্ছে...")

        csv_bytes = await asyncio.to_thread(results_to_csv_bytes, results)

        safe_title = re.sub(r"[^\w\u0980-\u09FF\-]+", "_", file_name.rsplit(".", 1)[0])[:50] or "ATLAS_QuestionBank"
        await send_document(chat_id, csv_bytes, f"ATLAS_{safe_title}.csv",
            caption=f"📚 Source: {source}\n📝 মোট MCQ: {len(results)}\n🚀 ATLAS APP",
            mime_type="text/csv")

        if loading_id:
            await tg_post("deleteMessage", {"chat_id": chat_id, "message_id": loading_id})

    except Exception as e:
        logger.error(f"[QCSV-AUTO] Error: {e}")
        await _safe_error_reply(chat_id, e)

# ============================================================
# MHTML/HTML → CSV — chorcha.net প্রশ্ন-উত্তর কে CSV এ export
# ============================================================
async def handle_qcsv_command(msg: dict):
    chat_id = msg["chat"]["id"]
    reply = msg.get("reply_to_message")

    if not reply or not reply.get("document"):
        await send_msg(chat_id,
            "❌ chorcha.net থেকে save করা .mhtml/.html file-এ reply করে /qcsv দাও!\n\n"
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
        parsed = await asyncio.to_thread(parse_mhtml_to_mcqs, raw_bytes, file_name)
        results = parsed["results"]
        source = parsed["source"] or "Unknown"

        if not results:
            if loading_id:
                await edit_msg(chat_id, loading_id, "❌ কোনো প্রশ্ন/উত্তর খুঁজে পাওয়া যায়নি! Format ভিন্ন হতে পারে।")
            return

        if loading_id:
            await edit_msg(chat_id, loading_id,
                f"✅ {len(results)} টি MCQ পাওয়া গেছে! ({source})\n📄 CSV বানানো হচ্ছে...")

        csv_bytes = await asyncio.to_thread(results_to_csv_bytes, results)

        safe_title = re.sub(r"[^\w\u0980-\u09FF\-]+", "_", file_name.rsplit(".", 1)[0])[:50] or "ATLAS_QuestionBank"
        await send_document(chat_id, csv_bytes, f"ATLAS_{safe_title}.csv",
            caption=f"📚 Source: {source}\n📝 মোট MCQ: {len(results)}\n🚀 ATLAS APP",
            mime_type="text/csv")

        if loading_id:
            await tg_post("deleteMessage", {"chat_id": chat_id, "message_id": loading_id})

    except Exception as e:
        logger.error(f"[QCSV] Error: {e}")
        await _safe_error_reply(chat_id, e)

# ============================================================
# /sheet — CSV file reply থেকে সরাসরি Practice Sheet PDF
# ============================================================
def _adapt_mcqs_for_print(mcqs: list) -> list:
    """Convert QuizBot mcq dicts (question/options list/answer letter/explanation)
    to AtlasMasterBot print-style data format."""
    BN = ['A', 'B', 'C', 'D', 'E']
    labels_map = {"A": 0, "B": 1, "C": 2, "D": 3}
    data = []
    for i, q in enumerate(mcqs):
        opts = q.get("options", ["", "", "", ""])
        opts = (opts + ["", "", "", ""])[:4]
        ans = q.get("answer", "A")
        ai = labels_map.get(str(ans).strip().upper(), 0) if not str(ans).isdigit() else int(ans) - 1
        data.append({
            'n': i + 1,
            'q': q.get('question', ''),
            'qi': '',
            'opts': opts,
            'oimgs': ['', '', '', ''],
            'exp': q.get('explanation', ''),
            'ei': '',
            'ai': ai,
            'al': BN[ai] if 0 <= ai < len(BN) else '?'
        })
    return data

_PRINT_CSS = """<style>
@page{size:A4 portrait;margin:10mm 10mm}
body{font-family:'Noto Sans Bengali','SolaimanLipi',Arial,sans-serif;font-size:13pt;line-height:1.2;color:#000;margin:0;padding:10px;width:210mm;max-width:210mm}
.exam-header{text-align:center;border:2px solid #4169E1;background-color:#F0F8FF;border-radius:6px;padding:10px;margin-bottom:15px}
.exam-header h1{color:#191970;margin:0;font-size:15pt;font-weight:bold}
.content-columns{column-count:2;column-gap:15px;column-fill:balance;column-rule:1px solid #ddd}
.question{margin-bottom:7px;break-inside:avoid;page-break-inside:avoid}
.question-header{margin-bottom:4px;display:flex;align-items:flex-start}
.question-num{font-weight:bold;color:#1E64B7;font-size:14pt;margin-right:5px;white-space:nowrap;flex-shrink:0}
.question-text{flex:1;line-height:1.6;font-size:15pt;color:#000;word-wrap:break-word}
.options-table-short{width:100%;border-collapse:collapse;margin:6px 0 6px 8px;table-layout:fixed}
.options-table-short td{border:none;padding:3px 8px 3px 0;vertical-align:top;font-size:15pt;color:#000;width:40%}
.options-table-short td.answer-col{display:flex;justify-content:center;align-items:center;font-weight:600;font-size:14pt;color:#000;padding-left:10px}
.answer-circle{font-weight:300;font-size:14pt;line-height:1}
.options-list{margin:6px 0 6px 8px;padding:0;list-style:none}
.options-list li{margin:2px 0;font-size:15pt;color:#000;word-wrap:break-word}
.option-with-answer{display:flex;justify-content:space-between;align-items:flex-start}
.explanation{margin:4px 0 2px 8px;padding:4px;color:#000;background-color:rgba(66,153,225,0.1);border-left:3px solid #4299e1;font-size:12pt;font-style:italic;break-inside:avoid}
.explanation-label{font-weight:bold;color:#2c5282}
.page-break{page-break-before:always;break-before:page}
.answers-section{column-count:1;margin-top:0}
.answer-table{width:100%;border-collapse:collapse;margin-top:0;border:1px solid #333}
.answer-table th,.answer-table td{border:1px solid #333;padding:6px;text-align:left;vertical-align:top;word-wrap:break-word}
.answer-table th{background-color:#f5f5f5;font-weight:bold;text-align:center;font-size:13pt}
.qno-col{width:8%;text-align:center}.ans-col{width:8%;text-align:center;font-weight:bold;font-size:14pt}.exp-col{width:84%;font-size:12pt}
.print-page{display:flex;flex-direction:column;min-height:277mm}
.answer-key-section{margin-top:auto;page-break-inside:avoid}
.answer-key-header{text-align:center;font-weight:bold;font-size:13pt;margin-bottom:10px;color:#000}
.answer-key-table{width:100%;border-collapse:collapse;border:1px solid #333;margin:0 auto}
.answer-key-table th,.answer-key-table td{border:1px solid #333;padding:6px;text-align:center;font-size:11pt}
.answer-key-table th{background-color:#f5f5f5;font-weight:bold}
img{max-width:35%!important;height:auto!important;vertical-align:middle}
</style>"""

def _check_short_option(opts):
    for v in opts:
        if v and len(str(v).strip()) > 16:
            return False
    return True

def _build_print_style1(data, heading):
    """Style 1: Study Material - Q + Options + inline Answer + Explanation"""
    body = f'<div class="exam-header"><h1>{heading} - Practice Sheet</h1></div><div class="content-columns">'
    for d in data:
        short = _check_short_option(d["opts"])
        body += f'<div class="question"><div class="question-header"><span class="question-num">{d["n"]:02d}.</span><div class="question-text">{d["q"]}</div></div>'
        ans_circle = f'[{d["al"]}]'
        if short:
            body += f'<table class="options-table-short"><tr><td>(A) {d["opts"][0]}</td><td>(B) {d["opts"][1]}</td><td rowspan="2" class="answer-col"><span class="answer-circle">{ans_circle}</span></td></tr><tr><td>(C) {d["opts"][2]}</td><td>(D) {d["opts"][3]}</td></tr></table>'
        else:
            body += f'<ul class="options-list"><li>(A) {d["opts"][0]}</li><li>(B) {d["opts"][1]}</li><li>(C) {d["opts"][2]}</li><li class="option-with-answer"><span>(D) {d["opts"][3]}</span><span class="answer-circle">{ans_circle}</span></li></ul>'
        if d['exp']:
            body += f'<div class="explanation"><span class="explanation-label">ব্যাখ্যা:</span> {d["exp"]}</div>'
        body += '</div>'
    body += '</div>'
    return f'<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">{_PRINT_CSS}</head><body>{body}</body></html>'

def _build_print_style2(data, heading):
    """Style 2: Exam Style - Questions page then separate Answer Table"""
    body = f'<div class="exam-header"><h1>{heading} - Questions</h1></div><div class="content-columns">'
    for d in data:
        short = _check_short_option(d["opts"])
        body += f'<div class="question"><div class="question-header"><span class="question-num">{d["n"]:02d}.</span><div class="question-text">{d["q"]}</div></div>'
        if short:
            body += f'<table class="options-table-short"><tr><td>(A) {d["opts"][0]}</td><td>(B) {d["opts"][1]}</td></tr><tr><td>(C) {d["opts"][2]}</td><td>(D) {d["opts"][3]}</td></tr></table>'
        else:
            body += f'<ul class="options-list"><li>(A) {d["opts"][0]}</li><li>(B) {d["opts"][1]}</li><li>(C) {d["opts"][2]}</li><li>(D) {d["opts"][3]}</li></ul>'
        body += '</div>'
    body += '</div><div class="page-break"></div><div class="answers-section"><table class="answer-table"><thead><tr><th class="qno-col">Q.No.</th><th class="ans-col">Ans</th><th class="exp-col">Explanation</th></tr></thead><tbody>'
    for d in data:
        body += f'<tr><td class="qno-col">{d["n"]}</td><td class="ans-col">{d["al"]}</td><td class="exp-col">{d["exp"] if d["exp"] else "-"}</td></tr>'
    body += '</tbody></table></div>'
    return f'<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">{_PRINT_CSS}</head><body>{body}</body></html>'

def _build_print_style3(data, heading, per_page=25):
    """Style 3: Compact Exam - paginated, each page's answer key directly below that page's questions"""
    pages_html = ""
    for start in range(0, len(data), per_page):
        chunk = data[start:start + per_page]
        body = f'<div class="print-page"><div class="exam-header"><h1>{heading}</h1></div><div class="content-columns" style="column-count:2;break-inside:avoid;">'
        for d in chunk:
            short = _check_short_option(d["opts"])
            body += f'<div class="question"><div class="question-header"><span class="question-num">{d["n"]}.</span><div class="question-text">{d["q"]}</div></div>'
            if short:
                body += f'<table class="options-table-short"><tr><td>(a) {d["opts"][0]}</td><td>(b) {d["opts"][1]}</td></tr><tr><td>(c) {d["opts"][2]}</td><td>(d) {d["opts"][3]}</td></tr></table>'
            else:
                body += f'<ul class="options-list"><li>(a) {d["opts"][0]}</li><li>(b) {d["opts"][1]}</li><li>(c) {d["opts"][2]}</li><li>(d) {d["opts"][3]}</li></ul>'
            body += '</div>'
        body += '</div><div class="answer-key-section"><div class="answer-key-header">সঠিক উত্তর যাচাই কর :)</div><table class="answer-key-table"><thead><tr><th>প্রশ্ন</th>'
        for d in chunk:
            body += f'<th>{d["n"]}</th>'
        body += '</tr></thead><tbody><tr><th>উত্তর</th>'
        for d in chunk:
            body += f'<td>{d["al"]}</td>'
        body += '</tr></tbody></table></div></div>'
        if start + per_page < len(data):
            body += '<div class="page-break"></div>'
        pages_html += body
    return f'<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">{_PRINT_CSS}</head><body>{pages_html}</body></html>'

PRINT_STYLE_BUILDERS = {
    "style1": _build_print_style1,
    "style2": _build_print_style2,
    "style3": _build_print_style3,
}
PRINT_STYLE_NAMES = {
    "style1": "🖨️ Style 1: Study Material",
    "style2": "🖨️ Style 2: Exam Style",
    "style3": "🖨️ Style 3: Compact Exam",
}

# ============================================================
# ATLASMASTERBOT /sheet SYSTEM — 100% PORTED (5 Reading formats)
# ============================================================
_AM_COLORS = {
    "header_bg": "#1B4F72", "header_text": "#FFFFFF",
    "question_bg": "#FFFBF0", "question_border": "#FBBF24",
    "qnum_bg": "#FEF3C7", "qnum_text": "#92400E",
    "option_bg": "#FFFFFF", "option_border": "#D1D5DB", "option_text": "#1F2937",
    "correct_bg": "#DCFCE7", "correct_border": "#4ADE80", "correct_text": "#14532D",
    "explanation_bg": "#EFF6FF", "explanation_border": "#4299E1", "explanation_text": "#1E40AF",
    "footer_text": "#6B7280",
}

_AM_FORMAT_NAMES = {
    'format_01': '📖 Practice Sheet (প্রশ্ন + উত্তর + ব্যাখ্যা)',
    'format_02': '📖 Solve Sheet (প্রশ্নপত্র + উত্তরপত্র)',
    'format_03': '📖 Exam Style (Answer টেবিল)',
    'format_04': '📖 Mixed Style (ইনলাইন উত্তর)',
    'format_05': '📖 Summary (Answer Key)',
}

def _am_fix_bn(text):
    if not text: return ""
    fixes = [('\u09C7\u09D7','\u09CC'),('\u09C7\u09BE','\u09CB'),('\u09BE\u09C7','\u09CB'),('\u09AF\u09BC','\u09DF'),('\u09A1\u09BC','\u09DC'),('\u09A2\u09BC','\u09DD')]
    for b,g in fixes: text=text.replace(b,g)
    return text

def _am_fix_chemical(text):
    if not text: return ""
    sub_map={'₀':'<sub>0</sub>','₁':'<sub>1</sub>','₂':'<sub>2</sub>','₃':'<sub>3</sub>','₄':'<sub>4</sub>','₅':'<sub>5</sub>','₆':'<sub>6</sub>','₇':'<sub>7</sub>','₈':'<sub>8</sub>','₉':'<sub>9</sub>'}
    sup_map={'⁰':'<sup>0</sup>','¹':'<sup>1</sup>','²':'<sup>2</sup>','³':'<sup>3</sup>','⁴':'<sup>4</sup>','⁵':'<sup>5</sup>','⁶':'<sup>6</sup>','⁷':'<sup>7</sup>','⁸':'<sup>8</sup>','⁹':'<sup>9</sup>','⁺':'<sup>+</sup>','⁻':'<sup>-</sup>'}
    for u,h in sub_map.items(): text=text.replace(u,h)
    for u,h in sup_map.items(): text=text.replace(u,h)
    return text

def _am_extract_images(text):
    return re.findall(r'src=["\'](https?://[^\s>"\']+)["\']', text)

def _am_download_image(url):
    try:
        import requests as _rq
        resp = _rq.get(url, timeout=10)
        if resp.status_code == 200:
            from PIL import Image as _PILImage
            from io import BytesIO as _BytesIO
            img = _PILImage.open(_BytesIO(resp.content))
            buf = _BytesIO(); img.save(buf, format='PNG')
            b64 = base64.b64encode(buf.getvalue()).decode()
            return f'<img src="data:image/png;base64,{b64}" style="max-width:100px;height:auto;display:block;margin:3px 0;">'
    except Exception:
        pass
    return ""

def _am_get_clean(text):
    if not text: return "", []
    text = str(text); imgs = _am_extract_images(text)
    text = re.sub(r'<img[^>]+>', '', text); text = re.sub(r'<[^>]+>', '', text)
    for s, d in [('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'), ('&nbsp;', ' ')]:
        text = text.replace(s, d)
    text = _am_fix_chemical(text); text = _am_fix_bn(text.strip())
    return text, imgs

def _am_get_css(watermark=""):
    wm = ""
    if watermark:
        wm = f'body::before{{content:"{watermark}";position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) rotate(-30deg);font-size:60pt;color:rgba(0,0,0,0.03);white-space:nowrap;z-index:999;pointer-events:none;font-weight:bold;letter-spacing:5px;}}'
    return f'''<style>
@page{{size:A4;margin:8mm;@bottom-right{{content:counter(page);font-size:7pt;color:{_AM_COLORS["footer_text"]}}}}}
body{{font-family:sans-serif;font-size:6.8pt;line-height:1.25;}}
.topic-bar-first{{text-align:center;background:linear-gradient(135deg,rgba(27,79,114,0.97),rgba(27,79,114,0.88));color:#fff;padding:10px 8px;font-size:16pt;font-weight:bold;backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);box-shadow:0 6px 20px rgba(0,0,0,0.25);margin-bottom:8px;border-radius:0 0 8px 8px;letter-spacing:1.5px;text-shadow:0 2px 4px rgba(0,0,0,0.3);}}
.footer-pg{{position:fixed;bottom:0;left:0;right:0;text-align:center;font-size:5.5pt;color:{_AM_COLORS["footer_text"]};padding:2px;z-index:100;background:#fff;border-top:1px solid #ddd;}}
.columns{{column-count:2;column-gap:6px;}}
.mcq{{break-inside:avoid;border:1px solid {_AM_COLORS["question_border"]};border-radius:5px;padding:3px;margin-bottom:2px;background:{_AM_COLORS["question_bg"]}}}
.qnum{{font-weight:bold;color:{_AM_COLORS["qnum_text"]};background:{_AM_COLORS["qnum_bg"]};padding:1px 3px;border-radius:3px;display:inline-block;margin-bottom:1px;font-size:6pt}}
.question{{font-weight:bold;margin-bottom:1px;font-size:7pt}}
.opt{{padding:1px 2px;margin:1px;border-radius:8px;background:{_AM_COLORS["option_bg"]};border:1px solid {_AM_COLORS["option_border"]};font-size:6.5pt;display:inline-block;color:{_AM_COLORS["option_text"]}}}
.opt-c{{background:{_AM_COLORS["correct_bg"]};border-color:{_AM_COLORS["correct_border"]};color:{_AM_COLORS["correct_text"]};font-weight:bold}}
.exp{{margin-top:1px;padding:2px;background:{_AM_COLORS["explanation_bg"]};border-left:2px solid {_AM_COLORS["explanation_border"]};font-size:6pt;color:{_AM_COLORS["explanation_text"]}}}
.ans-inline{{font-weight:bold;color:{_AM_COLORS["correct_text"]};font-size:7pt}}
table.at{{width:100%;border-collapse:collapse;margin-top:4px;font-size:6.5pt}}
table.at th,table.at td{{border:1px solid #555;padding:1px 2px;text-align:center}}
table.at th{{background:#f0f0f0}}
.answer-sidebar{{position:fixed;right:2mm;top:14mm;width:28mm;border:1px solid #333;padding:2px;font-size:5pt;background:#fff;z-index:10;max-height:80%;overflow-y:auto;box-shadow:0 2px 8px rgba(0,0,0,0.15);}}
.exp-table{{width:100%;border-collapse:collapse;margin-top:8px;font-size:6.5pt;page-break-before:always;}}
.exp-table th,.exp-table td{{border:1px solid #555;padding:2px;text-align:center;}}
.exp-table th{{background:#EFF6FF;}}
sub,sup{{font-size:0.65em}}
img{{max-width:80px;height:auto;display:block;margin:1px 0}}
{wm}
</style>'''

def _am_build_mcq_data(mcqs):
    """Convert MCQs (QuizBot's list-based options schema) to AtlasMasterBot's Sheet format."""
    data = []
    BN = ['A', 'B', 'C', 'D', 'E']
    for qi, mcq in enumerate(mcqs):
        q, qi_ = _am_get_clean(mcq.get('question', ''))
        e, ei_ = _am_get_clean(mcq.get('explanation', ''))
        opts_list = mcq.get('options', [])
        opts, oimgs = [], []
        for i in range(4):
            v = opts_list[i] if i < len(opts_list) else ''
            if v and str(v).strip():
                ct, ci = _am_get_clean(str(v))
                opts.append(ct)
                oimgs.append(''.join([_am_download_image(u) for u in ci]))
            else:
                opts.append('')
                oimgs.append('')
        ans_raw = str(mcq.get('answer', 'A')).strip().upper()
        ans_letter_map = {"A": 0, "B": 1, "C": 2, "D": 3, "1": 0, "2": 1, "3": 2, "4": 3}
        ai = ans_letter_map.get(ans_raw, 0)
        data.append({
            'n': qi + 1, 'q': q, 'qi': ''.join([_am_download_image(u) for u in qi_]),
            'opts': opts[:4], 'oimgs': oimgs[:4],
            'exp': e, 'ei': ''.join([_am_download_image(u) for u in ei_]),
            'ai': ai, 'al': BN[ai] if ai >= 0 else '?'
        })
    return data

def _am_build_html(data, heading, fmt, hdr_txt="", ftr_txt=""):
    """Build HTML — 100% AtlasMasterBot Sheet Bot logic (5 formats)."""
    css = _am_get_css()
    BN = ['A', 'B', 'C', 'D']
    body = ""
    tbl = ""

    if fmt == 1:
        body = f'<div class="topic-bar-first">{hdr_txt or heading}</div><div class="columns">'
        for d in data:
            body += f'<div class="mcq"><span class="qnum">Q.{d["n"]}</span><div class="question">{d["q"]}{d["qi"]}</div>'
            for oi in range(4):
                if d['opts'][oi]:
                    c = ['①', '②', '③', '④'][oi]
                    cl = 'opt opt-c' if oi == d['ai'] else 'opt'
                    body += f'<span class="{cl}">{c} {d["opts"][oi]}{d["oimgs"][oi]}</span> '
            if d['ai'] >= 0:
                body += f' <span class="ans-inline">✓ {BN[d["ai"]]}</span>'
            if d['exp']:
                body += f'<div class="exp">{d["exp"]}{d["ei"]}</div>'
            body += '</div>'
        body += '</div>'

    elif fmt == 2:
        per_page = 15
        pages = [data[i:i + per_page] for i in range(0, len(data), per_page)]
        body = ''
        for pg_idx, page_data in enumerate(pages):
            sidetbl = '<div class="answer-sidebar"><b>Ans</b><table style="font-size:5pt;width:100%;">'
            cols = min(len(page_data), 15)
            for i in range(0, cols, 2):
                sidetbl += '<tr>'
                sidetbl += f'<td>Q{page_data[i]["n"]}:{page_data[i]["al"]}</td>'
                if i + 1 < cols:
                    sidetbl += f'<td>Q{page_data[i+1]["n"]}:{page_data[i+1]["al"]}</td>'
                sidetbl += '</tr>'
            sidetbl += '</table></div>'
            body += sidetbl + f'<div style="margin-right:30mm;">'
            for d in page_data:
                body += f'<div class="mcq"><span class="qnum">Q.{d["n"]}</span><div class="question">{d["q"]}{d["qi"]}</div>'
                for oi in range(4):
                    if d['opts'][oi]:
                        body += f'<span class="opt">({BN[oi]}) {d["opts"][oi]}{d["oimgs"][oi]}</span> '
                body += '</div>'
            body += '</div>'
            if pg_idx < len(pages) - 1:
                body += '<div style="page-break-after:always;"></div>'
        tbl = '<div class="exp-table"><h3>📋 ব্যাখ্যা</h3><table><tr><th>Q.No</th><th>ব্যাখ্যা</th></tr>'
        for d in data:
            if d['exp']:
                tbl += f'<tr><td>Q{d["n"]}</td><td>{d["exp"]}{d["ei"]}</td></tr>'
        tbl += '</table></div>'

    elif fmt == 3:
        per_page = 15
        pages = [data[i:i + per_page] for i in range(0, len(data), per_page)]
        body = ''
        for pg_idx, page_data in enumerate(pages):
            body += '<div class="columns">'
            for d in page_data:
                body += f'<div class="mcq"><span class="qnum">Q.{d["n"]}</span><div class="question">{d["q"]}{d["qi"]}</div>'
                for oi in range(4):
                    if d['opts'][oi]:
                        body += f'<span class="opt">({BN[oi]}) {d["opts"][oi]}{d["oimgs"][oi]}</span> '
                body += '</div>'
            body += '</div>'
            body += '<div style="margin-top:2px"><b>Ans:</b><table class="at"><tr>'
            for d in page_data:
                body += f'<td>Q{d["n"]}</td>'
            body += '</tr><tr>'
            for d in page_data:
                body += f'<td><b>{d["al"]}</b></td>'
            body += '</tr></table></div>'
            if pg_idx < len(pages) - 1:
                body += '<div style="page-break-after:always;"></div>'

    elif fmt == 4:
        body = '<div class="columns">'
        for d in data:
            body += f'<div class="mcq"><span class="qnum">Q.{d["n"]}</span><div class="question">{d["q"]}{d["qi"]}</div>'
            for oi in range(4):
                if d['opts'][oi]:
                    body += f'<span class="opt">({BN[oi]}) {d["opts"][oi]}{d["oimgs"][oi]}</span> '
            body += f'<span class="ans-inline"> Ans: {d["al"]}</span>'
            if d['exp']:
                body += f'<div class="exp">{d["exp"]}{d["ei"]}</div>'
            body += '</div>'
        body += '</div>'

    elif fmt == 5:
        body = '<div class="columns">'
        for d in data:
            body += f'<div class="mcq"><span class="qnum">Q.{d["n"]}</span><div class="question">{d["q"]}{d["qi"]}</div>'
            for oi in range(4):
                if d['opts'][oi]:
                    body += f'<span class="opt">({BN[oi]}) {d["opts"][oi]}{d["oimgs"][oi]}</span> '
            body += '</div>'
        body += '</div>'
        tbl = '<div style="border:2px solid #1B4F72;padding:8px;margin-top:10px"><h3>📋 Answer Key</h3>'
        tbl += '<table class="at"><tr><th>Q.No</th><th>Ans</th><th>ব্যাখ্যা</th></tr>'
        for d in data:
            tbl += f'<tr><td>{d["n"]}</td><td><b>{d["al"]}</b></td><td>{d["exp"]}{d["ei"]}</td></tr>'
        tbl += '</table></div>'

    f = f'<div class="footer-pg">{ftr_txt or "Practice makes perfect"}</div>'
    return f'<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">{css}</head><body>{body}{tbl}{f}</body></html>'

_sheet_cache = {}

async def handle_sheet_command(msg: dict):
    chat_id = msg["chat"]["id"]
    reply = msg.get("reply_to_message")
    text = msg.get("text", "")
    parts = text.split(None, 1)
    custom_title = parts[1].strip() if len(parts) > 1 else None

    if not reply or not reply.get("document"):
        await send_msg(chat_id, "❌ CSV ফাইলে reply করে /sheet দাও!")
        return

    doc = reply["document"]
    file_name = doc.get("file_name", "")
    if not file_name.lower().endswith(".csv"):
        await send_msg(chat_id, "❌ শুধু .csv file support করে!")
        return

    loading = await send_msg(chat_id, "⏳ CSV পড়া হচ্ছে...")
    loading_id = loading.get("result", {}).get("message_id")

    try:
        csv_bytes = await download_tg_file(doc["file_id"])
        mcqs = _parse_csv_bytes(csv_bytes)

        if not mcqs:
            if loading_id:
                await edit_msg(chat_id, loading_id, "❌ CSV থেকে কোনো MCQ পাওয়া যায়নি! Format ঠিক আছে কিনা দেখো।")
            return

        if loading_id:
            await edit_msg(chat_id, loading_id, f"✅ {len(mcqs)} টি MCQ পাওয়া গেছে!\n🎨 Print Style বেছে নাও:")

        title = custom_title or "ATLAS Special"
        cache_key = f"{chat_id}:{loading_id}"
        _sheet_cache[cache_key] = {"mcqs": mcqs, "title": title}

        buttons = [[{"text": name, "callback_data": f"sheetstyle:{key}:{cache_key}"}]
                   for key, name in _AM_FORMAT_NAMES.items()]
        buttons += [[{"text": name, "callback_data": f"sheetstyle:{key}:{cache_key}"}]
                   for key, name in PRINT_STYLE_NAMES.items()]
        buttons.append([{"text": "📄 Default Style", "callback_data": f"sheetstyle:default:{cache_key}"}])

        if loading_id:
            await tg_post("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": loading_id,
                "reply_markup": {"inline_keyboard": buttons}
            })

    except Exception as e:
        logger.error(f"[SHEET] Error: {e}")
        await _safe_error_reply(chat_id, e)

import asyncio as _asyncio_sheet
_sheet_lock = _asyncio_sheet.Lock()

async def handle_sheet_style_callback(callback_query: dict):
    data = callback_query.get("data", "")
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]
    parts = data.split(":", 2)
    if len(parts) != 3:
        return
    _, style_key, cache_key = parts
    cached = _sheet_cache.get(cache_key)
    if not cached:
        await tg_post("answerCallbackQuery", {"callback_query_id": callback_query["id"], "text": "❌ Session expired, আবার /sheet দাও।", "show_alert": True})
        return

    mcqs, title = cached["mcqs"], cached["title"]
    await tg_post("answerCallbackQuery", {"callback_query_id": callback_query["id"]})

    style_label = _AM_FORMAT_NAMES.get(style_key) or PRINT_STYLE_NAMES.get(style_key, "Default")
    status_msg = await send_msg(chat_id, f"🎨 {style_label}\n⏳ 0% — শুরু হচ্ছে...")
    status_id = status_msg.get("result", {}).get("message_id")
    start_t = time.time()

    async def _progress(pct):
        if not status_id:
            return
        elapsed = time.time() - start_t
        try:
            await edit_msg(chat_id, status_id, f"🎨 {style_label}\n⏳ {pct}% — {elapsed:.1f}s")
        except Exception:
            pass

    try:
        async with _sheet_lock:
            await _progress(10)
            if style_key.startswith("format_"):
                am_data = _am_build_mcq_data(mcqs)
                fmt_num = int(style_key.split("_")[-1])
                html_out = _am_build_html(am_data, title, fmt_num, title)
            elif style_key == "default":
                html_out = _build_solve_sheet_html(title, 1, mcqs)
            else:
                data_adapted = _adapt_mcqs_for_print(mcqs)
                html_out = PRINT_STYLE_BUILDERS[style_key](data_adapted, title)

            pdf_bytes = await _html_to_pdf(html_out, progress_cb=_progress)
            pdf_bytes = await _apply_saved_watermark(pdf_bytes)

        if not pdf_bytes:
            await edit_msg(chat_id, status_id, "❌ PDF generate করতে সমস্যা হয়েছে!")
            try:
                await notify_owner(f"⚠️ /sheet PDF gen failed, style={style_key}, title={title}, mcqs={len(mcqs)}\nError: {_last_pdf_error['msg']}")
            except Exception:
                pass
            return

        safe_title = re.sub(r"[^\w\u0980-\u09FF\-]+", "_", title)[:50] or "ATLAS_Sheet"
        await send_document(chat_id, pdf_bytes, f"{safe_title}_sheet.pdf",
            caption=f"📖 Practice Sheet\n📝 মোট MCQ: {len(mcqs)}\n🚀 ATLAS APP")
        await tg_post("deleteMessage", {"chat_id": chat_id, "message_id": status_id})
    except Exception as e:
        logger.error(f"[SHEET STYLE] Error: {e}")
        if status_id:
            await edit_msg(chat_id, status_id, "❌ PDF generate করতে সমস্যা হয়েছে!")
        try:
            await notify_owner(f"⚠️ /sheet PDF gen exception: {str(e)[:300]}")
        except Exception:
            pass

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
body{{font-family:'Noto Sans Bengali',sans-serif;background:#fff;font-size:12.5px;}}
.hdr{{text-align:center;padding:10px 14px;background:#1a237e;color:#fff;margin-bottom:12px;border-radius:8px;}}
.hdr h1{{font-size:18px;font-weight:800;}}
.hdr .sub{{font-size:12.5px;color:#c5cae9;margin-top:3px;}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;}}
.card{{background:#fff;border:1.5px solid #c5cae9;border-radius:8px;padding:9px 10px;break-inside:avoid;page-break-inside:avoid;}}
.qno{{font-size:11.5px;font-weight:800;color:#1a237e;margin-bottom:3px;}}
.qtxt{{font-size:13.5px;font-weight:700;color:#111;margin-bottom:7px;line-height:1.6;}}
.opts-wrap{{display:flex;flex-direction:column;gap:3px;margin-bottom:7px;}}
.opt{{font-size:12.5px;color:#333;padding:2px 6px;border-radius:4px;border:1px solid #e0e0e0;line-height:1.5;}}
.opt.correct{{background:#e8f5e9;border-color:#43a047;color:#1b5e20;font-weight:700;}}
.opt.wrong{{background:#ffebee;border-color:#e53935;color:#b71c1c;font-weight:600;}}
.ans-row{{margin-bottom:4px;}}
.ans-badge{{font-size:11.5px;font-weight:800;color:#1b5e20;background:#f1f8e9;border:1px solid #81c784;border-radius:4px;padding:1px 7px;}}
.exp-box{{font-size:12px;color:#1a237e;background:#e8eaf6;border-left:3px solid #3949ab;padding:5px 7px;border-radius:0 5px 5px 0;line-height:1.55;}}
.footer{{text-align:center;font-size:10px;color:#9e9e9e;margin-top:12px;}}
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
            "<code>/pdf -p 1-5 -c @channel -m \"Topic\" [10]</code>\n"
            "<code>/pdf -p 2 -c -100xxx -t 447 -m \"Group Topic\" [10]</code>\n\n"
            "<code>[N]</code> = প্রতি পেইজে কতগুলো MCQ বানাতে হবে (ঐচ্ছিক)\n"
            "<code>-t</code> থ্রেড আইডি কোটেশন সহ/ছাড়া দুই ভাবেই দেওয়া যাবে"
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
    if params.get("mcq_count_min") and params.get("mcq_count_max"):
        mcq_count = (params["mcq_count_min"], params["mcq_count_max"])
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

        pdf_bytes = await _download_pdf_cached(file_id, chat_id=chat_id, message_id=reply["message_id"])

        if status_msg_id:
            await edit_msg(chat_id, status_msg_id,
                f"✅ Download complete!\n📄 File: {file_name}\n[██████████ 100%]\n⏳ PDF → Images converting...")

        # ── Auto-chunking: only when channel_id is already known (-c given).
        # If no channel_id, we must NOT bypass the channel-picker below —
        # so batching for that case is handled inside the no-channel branch.
        if channel_id and page_range:
            parts = page_range.split("-")
            req_first = int(parts[0])
            req_last = int(parts[1]) if len(parts) > 1 else req_first
        elif channel_id:
            total_pages = await asyncio.to_thread(get_pdf_page_count, pdf_bytes)
            req_first, req_last = 1, total_pages
        else:
            req_first = req_last = None

        if channel_id and (req_last - req_first + 1) > _PDF_MAX_PAGES_PER_CALL:
            for batch_start in range(req_first, req_last + 1, _PDF_MAX_PAGES_PER_CALL):
                batch_end = min(batch_start + _PDF_MAX_PAGES_PER_CALL - 1, req_last)
                batch_range = f"{batch_start}-{batch_end}"
                if status_msg_id:
                    await edit_msg(chat_id, status_msg_id,
                        f"⏳ Batch {batch_start}-{batch_end}/{req_last} processing...")
                ok, pages = await asyncio.to_thread(_render_pdf_cached, file_id, pdf_bytes, batch_range)
                if not ok:
                    await send_msg(chat_id, pages)
                    continue
                if not pages:
                    continue
                await process_pdf_pages(chat_id, uid, uname, pages, topic, mcq_count,
                    channel_id, False, file_name, None, thread_id=thread_id, skip_generate=False)
            return

        if channel_id:
            ok, pages = await asyncio.to_thread(_render_pdf_cached, file_id, pdf_bytes, page_range)
            # HF has 16GB RAM (not Render's constrained free tier) — safe to
            # keep pdf_bytes alive; also needed now for the render cache key.
            if not ok:
                await send_msg(chat_id, pages)
                return
            if not pages:
                await send_msg(chat_id, "❌ Page পাওয়া যায়নি!")
                return

            if status_msg_id:
                await edit_msg(chat_id, status_msg_id,
                    f"✅ {len(pages)} page পাওয়া গেছে!\n⏳ MCQ Generation শুরু হচ্ছে...")

        if not channel_id:
            # ── No -c given: decode + generate in RAM-safe batches of
            # _PDF_MAX_PAGES_PER_CALL pages at a time (avoids decoding the
            # entire PDF's pages into memory upfront for large docs).
            # Channel selection happens only AFTER all batches are generated.
            if page_range:
                parts = page_range.split("-")
                req_first = int(parts[0])
                req_last = int(parts[1]) if len(parts) > 1 else req_first
            else:
                total_pages = await asyncio.to_thread(get_pdf_page_count, pdf_bytes)
                req_first, req_last = 1, total_pages

            generated_pages = []
            for batch_start in range(req_first, req_last + 1, _PDF_MAX_PAGES_PER_CALL):
                batch_end = min(batch_start + _PDF_MAX_PAGES_PER_CALL - 1, req_last)
                batch_range = f"{batch_start}-{batch_end}"
                if status_msg_id:
                    await edit_msg(chat_id, status_msg_id,
                        f"⏳ Batch {batch_start}-{batch_end}/{req_last} generating MCQ...")
                ok, batch_pages = await asyncio.to_thread(_render_pdf_cached, file_id, pdf_bytes, batch_range)
                if not ok:
                    await send_msg(chat_id, batch_pages)
                    continue
                if not batch_pages:
                    continue
                batch_result = await pdf_generate_all_pages(
                    chat_id, batch_pages, topic, mcq_count, file_name, status_msg_id
                )
                generated_pages.extend(batch_result)
            pdf_bytes = None  # RAM fix: raw PDF no longer needed after all batches decoded

            channels = await db_get_channels()
            if not channels:
                await process_pdf_pages(chat_id, uid, uname, generated_pages, topic, mcq_count,
                    None, True, file_name, status_msg_id, thread_id=thread_id, skip_generate=True)
                return
            app.state.pdf_cache = getattr(app.state, "pdf_cache", {})
            app.state.pdf_cache[f"pdf_img_{uid}"] = generated_pages
            _cap_page_cache(app.state.pdf_cache)
            sb.table("quiz_sessions").upsert({
                "key": f"pdf_pending_{uid}",
                "data": json.dumps({"topic": topic, "mcq_count": mcq_count, "file_name": file_name, "status_msg_id": status_msg_id, "thread_id": thread_id, "file_id": file_id, "page_range": page_range}),
                "updated_at": int(time.time())
            }).execute()

            total_mcq_found = sum(len(mcqs) for _, _, mcqs in generated_pages)
            page_breakdown = "\n".join(
                f"📌 Page {fmt_page(p)}: {len(mcqs)} MCQ" for p, _, mcqs in generated_pages
            )
            kb = {"inline_keyboard": []}
            for ch in channels:
                ch_id = ch.get("channel_id", "")
                ch_name = ch.get("channel_name", ch_id)
                kb["inline_keyboard"].append([{"text": f"📢 {ch_name}", "callback_data": f"pdfch_{ch_id}_{uid}"}])
            kb["inline_keyboard"].append([{"text": "📄 CSV File Only", "callback_data": f"pdfch_csv_{uid}"}])
            await send_msg(chat_id,
                f"✅ Generation Complete! {total_mcq_found} MCQ পাওয়া গেছে ({len(generated_pages)} page)\n\n"
                f"{page_breakdown}\n\n"
                f"🎯 Topic: {topic}\n\nChannel select করো:",
                reply_markup=kb)
            return

        # ── Channel already known (-c flag) → stream per-page: ──
        # generate MCQ for a page and send its poll immediately,
        # instead of waiting for the whole PDF to finish generating.
        await process_pdf_pages(chat_id, uid, uname, pages, topic, mcq_count,
            channel_id, False, file_name, status_msg_id, thread_id=thread_id, skip_generate=False)
    except Exception as e:
        logger.error(f"[PDF] Handle error: {e}", exc_info=True)
        await _safe_error_reply(chat_id, e)
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

async def pdf_generate_all_pages(
    chat_id: int, pages: list, topic: str, mcq_count: int,
    file_name: str, status_msg_id: int = None
) -> list:
    """
    /qbm-এর মতোই Phase 1 -- channel select/posting-এর আগে সব page-এর MCQ
    generate করে ফেলে। Returns list of (page_num, img, mcqs) tuples.
    """
    page_status = [{"page": p, "done": False, "current": False, "mcq": 0} for p, _ in pages]
    start_time = time.time()
    results_by_idx = [None] * len(pages)
    _active_jobs["count"] = _active_jobs.get("count", 0) + 1
    set_active_job(chat_id, f"PDF MCQ generation ({file_name}, parallel)")

    # Concurrency cap: several pages generated at once instead of strictly
    # sequential — cuts wall-clock time roughly by this factor while staying
    # safe against provider rate limits (each page already does its own
    # internal key rotation across providers).
    _PDF_PARALLEL_PAGES = 4
    sem = asyncio.Semaphore(_PDF_PARALLEL_PAGES)
    lock = asyncio.Lock()
    total_mcq_box = {"n": 0}

    async def _run_one(idx, page_num, img):
        if is_cancelled(chat_id):
            return
        async with sem:
            if is_cancelled(chat_id):
                return
            async with lock:
                page_status[idx]["current"] = True
                if status_msg_id:
                    await edit_msg(chat_id, status_msg_id,
                        _build_dashboard(file_name, topic, pages, page_status, start_time, total_mcq_box["n"], 0))

            mcqs = []
            try:
                mcqs = await generate_mcq_from_image(img, topic, page_num, mcq_count)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[PDF Generate] Page {page_num} error: {e}; retrying once")

            # Strict no-page-miss guarantee: a page landing empty (crash or
            # every provider returned nothing) gets ONE retry before being
            # accepted as genuinely empty — protects against a single
            # transient failure silently dropping a whole page's MCQs.
            if not mcqs and not is_cancelled(chat_id):
                try:
                    await asyncio.sleep(1)
                    mcqs = await generate_mcq_from_image(img, topic, page_num, mcq_count)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"[PDF Generate] Page {page_num} retry also failed: {e}")

            if is_cancelled(chat_id):
                return
            async with lock:
                results_by_idx[idx] = (page_num, img, mcqs)
                total_mcq_box["n"] += len(mcqs)
                page_status[idx]["current"] = False
                page_status[idx]["done"] = True
                page_status[idx]["mcq"] = len(mcqs)
                if status_msg_id:
                    await edit_msg(chat_id, status_msg_id,
                        _build_dashboard(file_name, topic, pages, page_status, start_time, total_mcq_box["n"], 0))

    async def _watch_cancel(tasks):
        # Polls the cancel flag while pages are in flight; the moment /cancel
        # is issued, actively cancels every still-running page task instead
        # of letting gather() wait for all of them to finish naturally.
        while not all(t.done() for t in tasks):
            if is_cancelled(chat_id):
                for t in tasks:
                    if not t.done():
                        t.cancel()
                return
            await asyncio.sleep(0.3)

    try:
        if status_msg_id:
            await edit_msg(chat_id, status_msg_id,
                _build_dashboard(file_name, topic, pages, page_status, start_time, 0, 0))

        tasks = [
            asyncio.create_task(_run_one(idx, page_num, img))
            for idx, (page_num, img) in enumerate(pages)
        ]
        watcher = asyncio.create_task(_watch_cancel(tasks))
        await asyncio.gather(*tasks, return_exceptions=True)
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
    finally:
        _active_jobs["count"] = max(0, _active_jobs.get("count", 1) - 1)
        clear_active_job(chat_id)

    results = [r for r in results_by_idx if r is not None]
    return results


async def process_pdf_pages(
    chat_id: int, uid: int, uname: str,
    pages: list, topic: str, mcq_count: int,
    channel_id: str, csv_only: bool,
    file_name: str = "document.pdf",
    status_msg_id: int = None,
    thread_id: int = None,
    with_image: bool = True,
    skip_generate: bool = False
):
    _active_jobs["count"] = _active_jobs.get("count", 0) + 1
    try:
        return await _process_pdf_pages_inner(
            chat_id, uid, uname, pages, topic, mcq_count,
            channel_id, csv_only, file_name, status_msg_id,
            thread_id, with_image, skip_generate
        )
    finally:
        _active_jobs["count"] = max(0, _active_jobs.get("count", 1) - 1)

async def _process_pdf_pages_inner(
    chat_id: int, uid: int, uname: str,
    pages: list, topic: str, mcq_count: int,
    channel_id: str, csv_only: bool,
    file_name: str = "document.pdf",
    status_msg_id: int = None,
    thread_id: int = None,
    with_image: bool = True,
    skip_generate: bool = False
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

    page_status = [{"page": p[0], "done": False, "current": False, "mcq": (len(p[2]) if skip_generate else 0)} for p in pages]
    start_time = time.time()
    total_mcq = sum(len(p[2]) for p in pages) if skip_generate else 0
    total_polls = 0

    if not status_msg_id:
        r = await send_msg(chat_id, "⏳ Processing শুরু হচ্ছে...")
        status_msg_id = r.get("result", {}).get("message_id")

    await edit_msg(chat_id, status_msg_id,
        _build_dashboard(file_name, topic, pages, page_status, start_time, total_mcq, total_polls))

    summary_pages = []
    all_mcqs_csv = []
    first_image_msg_id = None
    _prefetch_task = None
    _prefetch_idx = None

    async def _gen_with_retry(img_, page_num_):
        """Page-level retry: try twice before giving up on a page entirely,
        so a single transient failure doesn't silently drop the whole page."""
        for _pg_attempt in range(2):
            try:
                _mcqs = await _generate_mcq_from_image_raw(img_, topic, page_num_, mcq_count)
                _mcqs = _cap_mcq_options(_mcqs, 4)
                if _mcqs:
                    return _mcqs
            except Exception as _pg_e:
                logger.warning(f"[PDF] Page {page_num_} gen attempt {_pg_attempt+1} failed: {_pg_e}")
            if _pg_attempt == 0:
                await asyncio.sleep(1)
        return []

    for idx, page_tuple in enumerate(pages):
        if skip_generate:
            page_num, img, mcqs = page_tuple
        else:
            page_num, img = page_tuple
        page_status[idx]["current"] = True
        await edit_msg(chat_id, status_msg_id,
            _build_dashboard(file_name, topic, pages, page_status, start_time, total_mcq, total_polls))

        try:
            if not skip_generate:
                # Speed fix: if the next page's generation was already
                # prefetched (started while this page's polls were being
                # sent), use that result instead of generating again.
                if _prefetch_task is not None and _prefetch_idx == idx:
                    mcqs = await _prefetch_task
                else:
                    mcqs = await _gen_with_retry(img, page_num)
                _prefetch_task = None
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
                    exp = m.get("explanation", "")
                    all_mcqs_csv.append([m["question"], opts[0], opts[1], opts[2], opts[3], ans_num, _strip_img_tag(exp), "1", "1"])
                await db_save_mcq_cache(cache_id, session_id, page_num, topic, mcqs)
            else:
                image_msg_id = None
                image_file_id = None
                if with_image:
                    caption = ""
                    if tag:
                        caption = f"{tag}\n\n"
                    caption += f"🟥ATLAS Special MCQ System\n🎯Topic: {topic}\n🌟Page No: {fmt_page(page_num)}"

                    photo_r = await send_photo(channel_id, img_bytes, caption, message_thread_id=thread_id)
                    if photo_r.get("ok"):
                        image_msg_id = photo_r["result"]["message_id"]
                        image_file_id = photo_r["result"]["photo"][-1]["file_id"]
                        if first_image_msg_id is None:
                            first_image_msg_id = image_msg_id
                            # Item 3: auto-pin the very first image of the job
                            await try_pin_message(channel_id, image_msg_id)

                mcqs = await _repair_thin_explanations(mcqs, img, topic)
                # explanation crop/attach removed — user requested no
                # explanation image after submit, for faster /pdf turnaround.

                # Speed fix: start generating the NEXT page's MCQs now, in the
                # background, while THIS page's polls are being sent below
                # (poll-sending is rate-limited/slow — generation can overlap
                # with it instead of waiting its turn after).
                if not skip_generate and idx + 1 < len(pages):
                    _next_page_num, _next_img = pages[idx + 1]
                    _prefetch_task = asyncio.create_task(_gen_with_retry(_next_img, _next_page_num))
                    _prefetch_idx = idx + 1

                poll_links = []
                first_poll_link = ""
                for i, mcq in enumerate(mcqs):
                  try:
                    opts = mcq.get("options", [])[:4]
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
                            explanation=exp,
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
                    await asyncio.sleep(0.35)
                  except Exception as _mcq_e:
                    logger.error(f"[Poll] MCQ {i+1} unexpected error, skipping: {_mcq_e}")
                    continue

                await db_save_mcq_cache(cache_id, session_id, page_num, topic, mcqs, poll_links, image_file_id, image_msg_id, channel_id)

                # FIX: summary_pages was declared but never populated — this is
                # what feeds the end-of-job summary message + its auto-pin
                # further down. Without this, the summary never sent.
                summary_pages.append({
                    "page": page_num,
                    "mcq_count": len(mcqs),
                    "first_poll": first_poll_link or "(লিংক পাওয়া যায়নি)"
                })

                exam_url = f"{GH_PAGES_EXAM_URL}?id={cache_id}"
                bot_un = await get_bot_username()
                quiz_url = f"https://t.me/{bot_un}?start=pdf_{cache_id}"
                poll_url = f"https://t.me/{bot_un}?start=poll_{cache_id}"
                new_quiz_url = f"https://t.me/{bot_un}?start=pdfnew_{cache_id}"
                new_poll_url = f"https://t.me/{bot_un}?start=pollnew_{cache_id}"

                end_data = {
                    "chat_id": channel_id,
                    "text": f"🚀Topic: {topic}\n🌟Page No: {fmt_page(page_num)}\n✅MCQ: {len(mcqs)}\n🔗First Poll Link:\n{first_poll_link}",
                    "reply_markup": {"inline_keyboard": [
                        [{"text": "📝 Quiz Solve", "url": quiz_url},
                         {"text": "🆕 New Quiz", "url": new_quiz_url}],
                        [{"text": "🔄 Poll Again", "url": poll_url},
                         {"text": "🆕 New Poll", "url": new_poll_url}],
                        [{"text": "🌐 Website Exam", "url": exam_url}]
                    ]},
                    "reply_to_message_id": image_msg_id
                }
                if thread_id:
                    end_data["message_thread_id"] = thread_id
                end_r = {"ok": False}
                for _end_attempt in range(3):
                    end_r = await tg_post("sendMessage", end_data)
                    if end_r.get("ok"):
                        break
                    logger.warning(f"[EndMsg] Page {page_num} attempt {_end_attempt+1} failed, retrying...")
                    await asyncio.sleep(2)
                if end_r.get("ok"):
                    await db_update_cache(cache_id, {"end_msg_id": end_r["result"]["message_id"]})
                else:
                    err_desc = end_r.get("description") or end_r.get("error") or "unknown"
                    logger.error(f"[EndMsg] Page {page_num} FINAL FAIL: {err_desc}")
                    if "reply" in str(err_desc).lower() or "not found" in str(err_desc).lower():
                        # Reply target message missing/deleted -> retry once without reply_to_message_id
                        end_data.pop("reply_to_message_id", None)
                        retry_r = await tg_post("sendMessage", end_data)
                        if retry_r.get("ok"):
                            await db_update_cache(cache_id, {"end_msg_id": retry_r["result"]["message_id"]})
                        else:
                            await notify_owner(f"⚠️ End message failed for page {fmt_page(page_num)}, topic: {topic}\nReason: {err_desc}")
                    else:
                        await notify_owner(f"⚠️ End message failed for page {fmt_page(page_num)}, topic: {topic}\nReason: {err_desc}")

                # /pdf on hole ending message er por auto Style1+Style3 Sheet PDF channel e jabe
                if await should_autosend_pdf(channel_id):
                    try:
                        data_adapted = _adapt_mcqs_for_print(mcqs)
                        reply_target = first_image_msg_id or image_msg_id
                        safe_title = re.sub(r"[^\w\u0980-\u09FF\-]+", "_", topic)[:50] or "ATLAS_Sheet"
                        _wm_saved = (await db_get_settings()).get("watermark", "")
                        for style_key in ("style1", "style3"):
                            html_s = PRINT_STYLE_BUILDERS[style_key](data_adapted, topic)
                            pdf_bytes = await _html_to_pdf(html_s)
                            if not pdf_bytes:
                                logger.error(f"[PDF-AUTOSEND] {style_key} generation returned empty for page {page_num}")
                                await notify_owner(f"⚠️ Auto-PDF ({style_key}) generate হয়নি — page {fmt_page(page_num)}, topic: {topic}")
                                continue
                            if _wm_saved:
                                try:
                                    pdf_bytes = add_watermark_to_pdf(pdf_bytes, _wm_saved)
                                except Exception as _wm_e:
                                    logger.warning(f"[PDF-AUTOSEND] watermark apply failed: {_wm_e}")
                            style_name = PRINT_STYLE_NAMES[style_key]
                            doc_r = await send_document(channel_id, pdf_bytes, f"{safe_title}_p{page_num}_{style_key}.pdf",
                                caption=f"📖 Practice Sheet ({style_name})\n🎯 Topic: {topic}\n🌟 Page: {fmt_page(page_num)}\n📝 মোট MCQ: {len(mcqs)}\n🚀 ATLAS APP",
                                message_thread_id=thread_id, reply_to_message_id=reply_target)
                            if doc_r and doc_r.get("ok"):
                                doc_msg_id = doc_r.get("result", {}).get("message_id")
                                if doc_msg_id:
                                    await try_pin_message(channel_id, doc_msg_id)
                            else:
                                err_desc = (doc_r or {}).get("description", "no response")
                                logger.error(f"[PDF-AUTOSEND] send_document failed ({style_key}): {err_desc}")
                                await notify_owner(f"⚠️ Auto-PDF ({style_key}) পাঠাতে ব্যর্থ — page {fmt_page(page_num)}\nReason: {err_desc}")
                    except Exception as e:
                        logger.error(f"[PDF-AUTOSEND] Error: {e}")
                        await notify_owner(f"⚠️ Auto-PDF সিস্টেমে error — page {fmt_page(page_num)}, topic: {topic}\nReason: {e}")

                for m in mcqs:
                    opts = m.get("options", ["", "", "", ""])
                    opts = [re.sub(r'^[A-Da-dক-ঘ][)\.।]\s*', '', str(o)) for o in opts]
                    ans_map = {"A": "1", "B": "2", "C": "3", "D": "4"}
                    ans_num = ans_map.get(m.get("answer", "A"), "1")
                    all_mcqs_csv.append([m["question"], opts[0], opts[1], opts[2], opts[3], ans_num, _strip_img_tag(m.get("explanation", "")), "1", "1"])

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
        finally:
            # RAM fix: each page's decoded PIL image (dpi=150) can be several MB.
            # Holding all pages of a batch in memory for the whole poll-sending
            # duration was causing OOM kills mid-send on low-RAM instances
            # (looked like the bot dying / auto-restarting mid-channel-post).
            # Drop this page's image the moment we're done with it.
            try:
                pages[idx] = (page_num, None, None) if skip_generate else (page_num, None)
            except Exception:
                pass
            img = None

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
            summary += f"🌟Page-{fmt_page(p['page'])} ({p['mcq_count']} MCQ):\n{p['first_poll']}\n"
        summary += (
            f"\n💥শুভকামনা প্রিয় শিক্ষার্থী {uname}...\n"
            '"যেকোনো প্রশ্ন থাকলে মেসেজ দাও "Ask Your Mentor" গ্রুপে।\n'
            "🚀Whatsapp Helpline: wa.me/8801999681290\n🔗Website: Atlascourses.com"
        )
        summary_data = {"chat_id": channel_id, "text": summary, "disable_web_page_preview": True}
        if first_image_msg_id:
            summary_data["reply_to_message_id"] = first_image_msg_id
        sum_r = await tg_post("sendMessage", summary_data)
        # Auto-pin the summary message (same as /csvS master summary behavior)
        if sum_r.get("ok"):
            await try_pin_message(channel_id, sum_r["result"]["message_id"])

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
    if params.get("mcq_count_min") and params.get("mcq_count_max"):
        mcq_count = (params["mcq_count_min"], params["mcq_count_max"])
    thread_id = params["thread_id"]

    file_id = reply["document"]["file_id"]
    file_name = reply["document"].get("file_name","document.pdf")
    file_size = reply["document"].get("file_size",0)

    status_r = await send_msg(chat_id, "⏳ PDF download হচ্ছে...")
    status_msg_id = status_r.get("result",{}).get("message_id")

    try:
        pdf_bytes = await _download_pdf_cached(file_id, chat_id=chat_id, message_id=reply["message_id"])
        ok, pages = await asyncio.to_thread(_render_pdf_cached, file_id, pdf_bytes, page_range)
        if not ok:
            await send_msg(chat_id, pages)
            return

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
            _cap_page_cache(app.state.pdf_cache)
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
        await _safe_error_reply(chat_id, e)

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
        "mcq_count": None,
        "mcq_count_min": None,
        "mcq_count_max": None
    }

    m = re.search(r'-p\s+([\d,\-]+)', text)
    if m:
        result["page_range"] = m.group(1)

    m = re.search(r'-c\s+(@\S+|-100\d+)', text)
    if m:
        result["channel_id"] = m.group(1)

    m = re.search(r'-m\s+"([^"]+)"', text) or re.search(r"-m\s+'([^']+)'", text) or re.search(r'-m\s+(\S+)', text)
    if m:
        result["topic"] = m.group(1)

    m = re.search(r'-t\s+(\d+)', text)
    if m:
        result["thread_id"] = int(m.group(1))

    m_range = re.search(r'\[(\d+)\s*-\s*(\d+)\]', text)
    if m_range:
        lo, hi = int(m_range.group(1)), int(m_range.group(2))
        result["mcq_count_min"] = min(lo, hi)
        result["mcq_count_max"] = max(lo, hi)
    else:
        m_bracket = re.search(r'\[(\d+)\]', text)
        if m_bracket:
            result["mcq_count"] = int(m_bracket.group(1))
        else:
            cleaned = re.sub(r'-[pcmt]\s+\S+', '', text)
            m2 = re.search(r'(\d+)\s*$', cleaned)
            if m2:
                result["mcq_count"] = int(m2.group(1))

    return result

# Item 10: per-user serialization — if a user fires multiple /pdf, /qbm, /pdfm
# jobs at the same time, queue them so each finishes in order instead of racing
# (which caused RAM spikes / corrupted interleaved output).
_PDFM_USER_LOCKS: dict = {}
_PDFM_USER_QUEUE_LEN: dict = {}

def _get_pdfm_lock(uid: int) -> asyncio.Lock:
    lock = _PDFM_USER_LOCKS.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _PDFM_USER_LOCKS[uid] = lock
    return lock

async def process_pdfm_pages(
    chat_id: int, uid: int, uname: str,
    pages: list, topic: str, mcq_count,
    channel_id, csv_only: bool,
    file_name: str = "document.pdf",
    status_msg_id: int = None,
    thread_id: int = None
):
    lock = _get_pdfm_lock(uid)
    if lock.locked():
        _PDFM_USER_QUEUE_LEN[uid] = _PDFM_USER_QUEUE_LEN.get(uid, 0) + 1
        pos = _PDFM_USER_QUEUE_LEN[uid]
        try:
            await send_msg(chat_id, f"⏳ আগের PDF/PPT কাজ শেষ হচ্ছে... তোমার এই request queue তে #{pos} নম্বরে আছে, একে একে সব হয়ে যাবে।")
        except Exception:
            pass
    async with lock:
        _PDFM_USER_QUEUE_LEN[uid] = max(0, _PDFM_USER_QUEUE_LEN.get(uid, 1) - 1)
        return await _process_pdfm_pages_impl(
            chat_id, uid, uname, pages, topic, mcq_count,
            channel_id, csv_only, file_name, status_msg_id, thread_id
        )


async def _process_pdfm_pages_impl(
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
    set_active_job(chat_id, f"PDFM MCQ generation + Poll posting ({file_name}, page-by-page)")

    for idx, (page_num, img) in enumerate(pages):
        if is_cancelled(chat_id):
            clear_active_job(chat_id)
            break
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
                        ans_num, _strip_img_tag(m.get("explanation","")),"1","1"])
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
                        # Item 3: auto-pin the very first image of the job
                        await try_pin_message(channel_id, image_msg_id)

                poll_links = []
                first_poll_link = ""
                for i, mcq in enumerate(mcqs):
                    opts = mcq.get("options",[])[:4]
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
                            explanation=exp,
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
                    await asyncio.sleep(1.0)

                await db_save_mcq_cache(cache_id, session_id, page_num, topic, mcqs,
                    poll_links, image_file_id, image_msg_id, channel_id)

                end_text = (
                    f"🚀Topic: {topic}\n"
                    f"🌟Page No: {fmt_page(page_num)}\n"
                    f"✅MCQ: {len(mcqs)}\n"
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

                # /pdf on hole ending message er por auto Style1+Style3 Sheet PDF channel e jabe
                if await should_autosend_pdf(channel_id):
                    try:
                        data_adapted = _adapt_mcqs_for_print(mcqs)
                        reply_target = first_image_msg_id or image_msg_id
                        safe_title = re.sub(r"[^\w\u0980-\u09FF\-]+", "_", topic)[:50] or "ATLAS_Sheet"
                        _wm_saved = (await db_get_settings()).get("watermark", "")
                        for style_key in ("style1", "style3"):
                            html_s = PRINT_STYLE_BUILDERS[style_key](data_adapted, topic)
                            pdf_bytes = await _html_to_pdf(html_s)
                            if not pdf_bytes:
                                logger.error(f"[PDF-AUTOSEND] {style_key} generation returned empty for page {page_num}")
                                await notify_owner(f"⚠️ Auto-PDF ({style_key}) generate হয়নি — page {fmt_page(page_num)}, topic: {topic}")
                                continue
                            if _wm_saved:
                                try:
                                    pdf_bytes = add_watermark_to_pdf(pdf_bytes, _wm_saved)
                                except Exception as _wm_e:
                                    logger.warning(f"[PDF-AUTOSEND] watermark apply failed: {_wm_e}")
                            style_name = PRINT_STYLE_NAMES[style_key]
                            doc_r = await send_document(channel_id, pdf_bytes, f"{safe_title}_p{page_num}_{style_key}.pdf",
                                caption=f"📖 Practice Sheet ({style_name})\n🎯 Topic: {topic}\n🌟 Page: {fmt_page(page_num)}\n📝 মোট MCQ: {len(mcqs)}\n🚀 ATLAS APP",
                                message_thread_id=thread_id, reply_to_message_id=reply_target)
                            if doc_r and doc_r.get("ok"):
                                doc_msg_id = doc_r.get("result", {}).get("message_id")
                                if doc_msg_id:
                                    await try_pin_message(channel_id, doc_msg_id)
                            else:
                                err_desc = (doc_r or {}).get("description", "no response")
                                logger.error(f"[PDF-AUTOSEND] send_document failed ({style_key}): {err_desc}")
                                await notify_owner(f"⚠️ Auto-PDF ({style_key}) পাঠাতে ব্যর্থ — page {fmt_page(page_num)}\nReason: {err_desc}")
                    except Exception as e:
                        logger.error(f"[PDF-AUTOSEND] Error: {e}")
                        await notify_owner(f"⚠️ Auto-PDF সিস্টেমে error — page {fmt_page(page_num)}, topic: {topic}\nReason: {e}")

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
                        ans_num, _strip_img_tag(m.get("explanation","")),"1","1"])

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

    clear_active_job(chat_id)

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
# FEATURE: /qbm — Question Bank Maker (EXTRACT existing MCQ from PDF)
# Ported 100% from AtlasMasterBot's qbm_handler + process_pdf(is_qbm=True)
# Different from /pdfm: extracts MCQs that ALREADY EXIST in the PDF,
# never generates new ones. OCR fallback for scanned PDFs. 3x retry per page.
# ============================================================
QBM_EXTRACT_PROMPT_DEFAULT = """YOU ARE A STRICT MCQ EXTRACTOR OPERATING IN A SPECIAL PERMANENT MODE. YOUR ONLY JOB IS TO EXTRACT MCQs THAT ALREADY EXIST ON THIS PAGE. YOU NEVER INVENT NEW QUESTIONS. FOLLOW EVERY RULE BELOW WITHOUT A SINGLE EXCEPTION, ALWAYS, ON EVERY PAGE, EVERY TIME.

════════════════════════════════
🔴 ABSOLUTE FORBIDDEN RULES (ZERO TOLERANCE)
════════════════════════════════
❌ NEVER create a new question from any text, fact, or information on the page
❌ NEVER add even ONE extra MCQ beyond what already exists on the page/image
❌ NEVER skip any existing MCQ — extract ALL of them, serially, in the exact order they appear
❌ NEVER guess an answer — only detect it from actual image/page content
❌ NEVER modify question or option text (only remove numbering prefixes)
❌ If the page has ZERO existing MCQs → output EXACTLY [] (empty array). Do NOT invent a single MCQ.
❌ If the page has exactly N existing MCQs → output EXACTLY those N. Never more, never fewer, never a "similar" or "extra" one.
❌ No question count is ever given to you and none is ever needed — extract however many genuinely exist, nothing else.
❌ This is a PERMANENT, ALWAYS-ON extraction mode — these rules apply identically to every page, every call, no matter what.

════════════════════════════════
📌 EXTRACTION RULES
════════════════════════════════
✅ Extract ALL MCQs that already exist on this page — Bangla, English, or mixed language
✅ Extract from any font style — printed, handwritten, bold, italic
✅ Extract from blurry, low quality, rotated, or scanned images
✅ Perform MULTIPLE independent internal read-throughs of the page (at least 3) and
   cross-check your own extraction before finalizing, so no existing MCQ is missed or misread.
   Pay special attention to the LAST MCQ on the page/column — it is the most commonly missed one.
   After the draft list is built, count the visible MCQs on the page and verify your list length
   matches that count exactly before finalizing.
✅ Remove question numbering only: (১., 1., Q1., Q.1, ক., a.) from question text
✅ Keep original question and option wording intact (do not paraphrase or rewrite existing text)
✅ If any obvious spelling mistake is seen, correct it — but do not alter meaning

════════════════════════════════
🎯 ANSWER DETECTION (ALL FORMATS) — triple-check before finalizing
════════════════════════════════
The correct answer MUST come from an actual source found in the page/image content.
NEVER pick/guess an answer yourself — the answer must always be traceable to one of
the source types below. Scan for ALL of these possible answer sources, in this order
of likelihood, before concluding no answer exists:

Source A — Answer marked directly on an option: circle, tick (✓), cross(✗)-elimination,
  underline, bold, highlight, star (★), or any other visual mark on one option
  🔴 ABSOLUTE PRIORITY: if ANY option on the page has ANY visual mark on it — no matter
  which type (tick/circle/cross/underline/bold/highlight/star/anything else) — that marked
  option IS the answer, 100%, strictly, with zero exception. Do NOT second-guess it, do NOT
  cross-check it against an answer key elsewhere, and do NOT let a separate answer-key table
  (Source C/D/E) override or contradict a mark that is physically on the option itself. A
  mark directly on an option is ground truth and wins over every other source, always.
Source B — Answer given immediately with/after the MCQ itself (right after the question
  block, before the next question starts)
Source C — Answer table/box at the BOTTOM of the SAME page: a small table, boxed list,
  or line like "Answer: 1-A, 2-C, 3-B..." — match question number → correct option
  (only use this when NO mark exists directly on any option for that question)
Source D — Combined/consolidated answer key appearing SEVERAL PAGES LATER (not
  necessarily the very next page — scan forward through ALL available pages, since many
  question banks group all answers together after 2-3 pages of questions, or at the very
  end of the document): match question number exactly → correct option
  (only use this when NO mark exists directly on any option for that question)
Source E — Answer key on the page(s) immediately BEFORE or AFTER this one, in any of
  the above formats (marked option, inline, or boxed table)
  (only use this when NO mark exists directly on any option for that question)

Rules while scanning:
→ 🔴 PRIORITY ORDER IS ABSOLUTE: Source A (mark on option) > Source B > Source C > Source D
  > Source E. If a mark exists directly on an option, stop right there — that is the answer,
  do not continue scanning other sources for that question.
→ Check every source type above before deciding an answer is missing — the answer for a
  question on this page may live on a completely different page from the ones you've
  processed so far, so scan broadly, not just this single page.
→ Match strictly by question number (or exact question text if numbers are unclear/reused).
→ NEVER invent, guess, or default an answer yourself under any circumstance.
→ If — and only if — you have scanned all available pages/sources and genuinely found NO
  answer indication anywhere for that specific question → set answer as "A" and note in
  explanation "Answer not found in source". This is the last resort, never the first choice.
→ Convert whatever format the source uses (number, checkmark, circled letter, bold option,
  etc.) into the standard A/B/C/D letter for output.
→ Re-verify each detected answer against its source at least twice before finalizing —
  a wrong answer is worse than a missing one, so confirm carefully.

════════════════════════════════
🎯 OPTION ORDER (ABSOLUTE, ZERO-TOLERANCE — কখনো শাফল/পুনর্বিন্যাস/re-sort করবে না)
════════════════════════════════
- পেজে option যেই label সিস্টেমেই থাকুক (A,B,C,D / a,b,c,d / ক,খ,গ,ঘ / ১,২,৩,৪ / বুলেট/কোনো
  label ছাড়া top-to-bottom বা left-to-right) — output-এ ঠিক সেই ভিজ্যুয়াল/সোর্স পজিশনের
  ক্রমেই ১ম, ২য়, ৩য়, ৪র্থ option বসাবে output schema-র A,B,C,D slot-এ। Source-এর ১ম
  option → output A slot, ২য় → B slot, ৩য় → C slot, ৪র্থ → D slot। এটা label matching নয়,
  POSITION matching — সোর্সের label যা-ই হোক (a/ক/1/bullet), তার পজিশনই সিদ্ধান্তকারী।
- Option-এর টেক্সট কখনো reorder/sort/rearrange করবে না (বর্ণানুক্রমিক সাজানো, মান অনুযায়ী
  সাজানো — কোনোভাবেই না) — সোর্সে যেই sequence-এ ছিল ঠিক সেই sequence অক্ষুণ্ণ রাখবে।
- Option সিরিয়াল ঠিকভাবে (স্ট্রিক্টলি পজিশন ম্যাচ করে) রাখা হলে answer letter ও স্বয়ংক্রিয়ভাবে
  সঠিক সিরিয়ালেই পাওয়া যাবে — কারণ answer letter নির্ধারণ করা হয় "সঠিক উত্তরটি output-এর কোন
  position-এ আছে" তার ভিত্তিতে, সোর্সের original label-এর ভিত্তিতে না।
  উদাহরণ: সোর্সে option ক্রম গ,খ,ক,ঘ থাকলে এবং সঠিক উত্তর সোর্সের "ক" হলে — output-এ ক পজিশন
  ৩ নম্বরে থাকবে (output slot C), তাই answer = "C" (পজিশন অনুযায়ী), "A" নয়।
- প্রতিটা MCQ finalize করার আগে ৩ ধাপে verify করো (STRICT, SKIP করা যাবে না):
  ধাপ ১: output-এর ৪টা option স্লট সোর্সের ৪টা option-এর পজিশন অনুযায়ী সঠিক কি না চেক করো।
  ধাপ ২: সঠিক উত্তরের টেক্সট output-এর কোন slot-এ (A/B/C/D) বসেছে খুঁজে বের করো।
  ধাপ ৩: answer letter ঠিক সেই slot-কেই নির্দেশ করছে কি না নিশ্চিত করো — অমিল থাকলে ঠিক করো।
- সংখ্যা/সাল/তারিখ (Bengali সংখ্যা যেমন ১৯৭৬ বা English সংখ্যা যেমন 1976) অক্ষত হুবহু রাখবে —
  Bengali সংখ্যাকে English-এ বা English সংখ্যাকে Bengali-তে কখনো convert করবে না। প্রতিটা
  সংখ্যা সোর্সের সাথে digit-by-digit মিলিয়ে verify করবে (৯↔9, ৬↔6 গুলিয়ে ফেলা কড়াভাবে নিষিদ্ধ)।

════════════════════════════════
📖 উদ্দীপক (PASSAGE/STIMULUS) HANDLING — STRICT, ALWAYS ACTIVE
════════════════════════════════
- যদি কোনো প্রশ্ন বা প্রশ্নগোষ্ঠীর আগে একটা উদ্দীপক (passage/stimulus/scenario paragraph) থাকে,
  সেই উদ্দীপকটি প্রথমে identify করবে এবং তার সাথে যুক্ত প্রতিটা MCQ-কে উদ্দীপকের সাথে reply/link
  করেই ধরবে — অর্থাৎ output-এ প্রতিটা সংশ্লিষ্ট MCQ-র question টেক্সটের শুরুতে সেই উদ্দীপকের
  পূর্ণ টেক্সট জুড়ে দিতে হবে, তারপর তার নিচে সেই নির্দিষ্ট MCQ-র প্রশ্ন — যাতে প্রতিটা MCQ standalone
  ভাবে বোঝা যায় (উদ্দীপক ছাড়া প্রশ্নটা অসম্পূর্ণ থাকা উচিত নয়)।
- একই উদ্দীপকের অধীনে একাধিক MCQ থাকলে প্রতিটাতেই সেই একই উদ্দীপক পুনরায় জুড়ে দিতে হবে (কপি
  করে), প্রতিটা MCQ আলাদা আলাদা ভাবে সম্পূর্ণ (self-contained) থাকতে হবে।
- উদ্দীপক শনাক্তকরণে সতর্ক থাকবে: সাধারণ প্রশ্নের সাথে উদ্দীপক-ভিত্তিক প্রশ্ন গুলিয়ে ফেলবে না —
  passage/scenario/case-study টাইপ কনটেন্ট যা একাধিক প্রশ্নের বেস হিসেবে কাজ করছে, সেটাই উদ্দীপক।

════════════════════════════════
💡 EXPLANATION RULES (STRICT PRIORITY ORDER — follow exactly, always, in this order)
════════════════════════════════
🔴 ABSOLUTE TOP PRIORITY — READ FIRST:
If the page/image contains ANY explanation/answer-reasoning/ব্যাখ্যা text that is directly
attached to, written below, or clearly associated with this specific MCQ — you MUST use
that EXACT text as the explanation, with ZERO exceptions. This overrides every other rule
in this section, including the character-length limit below. Do NOT summarize, shorten,
paraphrase, translate, "clean up", or improve it in ANY way — copy it byte-for-byte,
word-for-word, character-for-character, EXACTLY as it appears in the source (same spelling,
same punctuation, same wording, same everything). This is a 100% verbatim, same-to-same,
mandatory copy — never a rewritten or condensed version. Only if this case does not apply
(no explanation exists anywhere near/for this MCQ) do you move to case 2 below.

1) If the MCQ already has an explanation/answer-reasoning written directly below or attached
   to it on the page → copy that explanation 100% VERBATIM, word-for-word, character-for-
   character, EXACTLY as written in the source — same spelling, same punctuation, same
   wording. Do NOT paraphrase, shorten, rewrite, "improve", or apply the 165-character limit
   to this case — the source explanation is used as-is regardless of its length. This is the
   single highest-priority rule in this entire section and is NEVER skipped when applicable.
2) Else if there is no explanation directly under the MCQ, but the page contains other
   relevant information related to this MCQ's topic (a paragraph, note, box, table, or fact
   elsewhere on the page/related pages that relates to this question) → build the explanation
   using that relevant information, stated as direct fact (see forbidden-phrase rule below).
   Max 165 characters, Bengali language, factually accurate.
3) Else if there is no explanation anywhere and no relevant info anywhere on the page/source
   related to this MCQ → then, and ONLY then, generate the BEST, most relevant, factually
   accurate explanation yourself from your own real knowledge.
   Max 165 characters, Bengali language, factually accurate.
- Whichever of the 3 cases applies, the explanation content must always convey: why the
  correct option is correct, AND brief relevant info tied to why the other options are
  wrong/related context — except in case 1, where you copy the source explanation exactly
  as-is even if it doesn't explicitly cover the wrong options and even if it exceeds 165 chars.
- This priority order (1 → 2 → 3) is permanent and always active — never skip a step or
  reorder it, on every single MCQ, every time. Case 1 (verbatim source copy) is checked FIRST
  for every MCQ, before considering generating any explanation yourself.

════════════════════════════════
🧮 MATH / CHEMISTRY FORMATTING (MANDATORY, ALWAYS ACTIVE — question, options, AND explanation)
════════════════════════════════
This rule is PERMANENTLY ON for every MCQ produced, with no exceptions, regardless of subject:
- Always use proper Unicode subscript characters for chemical formula quantities and
  proper Unicode superscript characters for exponents/powers/ionic charges — NEVER raw
  underscore/caret notation, NEVER plain inline digits where a subscript/superscript belongs.
- Chemical formulas: subscript quantity numbers correctly.
  Correct: H₂O, CO₂, NaHCO₃, H₂SO₄, Ca(OH)₂, Fe₂O₃, C₆H₁₂O₆
  Wrong: H2O, CO2, NaHCO3, H2SO4 (never output these)
- Ionic charges/oxidation states: use superscript with correct sign.
  Correct: Na⁺, Ca²⁺, Fe³⁺, Cl⁻, SO₄²⁻, O²⁻
- Exponents/powers/scientific notation: superscript the exponent.
  Correct: x², 10³, a⁻¹, E=mc², 6.02×10²³, v₀, xₙ
  Wrong: x^2, 10^3, x_0 (never output caret/underscore literally)
- Units, degree symbols, and multiplication signs must be correctly formatted: °C, °F, m/s²,
  cm³, kg·m/s², use × not x for multiplication in scientific/math contexts.
- Apply this identically and consistently across the question text, all four options, AND
  the explanation — never mix correct and incorrect formatting within the same MCQ.
- Double-check every number adjacent to a letter/formula/exponent before finalizing output:
  if it should be a subscript or superscript, it MUST be rendered as one, always.

════════════════════════════════
🚫 FORBIDDEN SOURCE-REFERENCE PHRASES (PERMANENT, ALWAYS ACTIVE — question AND explanation)
════════════════════════════════
NEVER, under any circumstances, in the question text OR the explanation text, use any of
these phrase patterns (or their Bengali equivalents, or any semantically similar phrase)
that refer back to the source material itself instead of stating the fact directly:
❌ "উল্লেখিত চিত্রে" / "চিত্রে দেখা যাচ্ছে" / "বক্সে" / "ছকে" / "উদ্দীপকে" / "সারণিতে" /
   "টপিকে" / "পৃষ্ঠা নং এ" / "পৃষ্ঠায়" / "প্যাসেজে" / "অনুচ্ছেদে" / "লেখচিত্রে" / "গ্রাফে"
❌ "দেখা যাচ্ছে" / "বলা আছে" / "উল্লেখ করা আছে" / "উল্লেখ আছে" / "লক্ষ করা যায়" /
   "বর্ণনা আছে" / "দেখানো হয়েছে" / "দেওয়া আছে" / "প্রদত্ত" / "উপরে দেখানো"
❌ Any English equivalents: "as shown in the figure/box/table/diagram/passage", "shown above",
   "mentioned in the text/page", "as given", "according to the figure/table/passage above"
❌ Any phrase — in any language, any wording — that talks ABOUT the source (image/box/table/
   diagram/passage/page number/graph) instead of stating the fact/content directly and plainly.
Instead: ALWAYS state the actual fact, information, or content directly and naturally, as if
it were plain general knowledge — NEVER mention or imply that it came from "the shown
image/box/table/passage/page". This rule applies permanently, always, to every single MCQ's
question and explanation, with absolutely no exceptions, regardless of subject or source type.

════════════════════════════════
📤 OUTPUT FORMAT
════════════════════════════════
Output ONLY a valid JSON array. No extra text. No markdown. No explanation outside JSON.
If NO MCQ exists on this page → return exactly: []

[{"question":"...","options":{"A":"...","B":"...","C":"...","D":"..."},"answer":"A/B/C/D","explanation":"... (max 165 chars Bengali)"}]"""


# ── QBM PERMANENT PROMPT MEMORY ──
# Active prompt cached in-process; persisted in Supabase (quiz_sessions,
# key="qbm_active_prompt") so it survives restarts. New page-এ গেলে DB আবার
# পড়তে হয় না — RAM cache-এ read, update হলেই DB-তে ও RAM-এ একসাথে save হয়।
_qbm_prompt_cache = {"prompt": None}

def qbm_get_active_prompt() -> str:
    if _qbm_prompt_cache["prompt"]:
        return _qbm_prompt_cache["prompt"]
    try:
        r = sb.table("quiz_sessions").select("data").eq("key", "qbm_active_prompt").execute()
        if r.data:
            p = json.loads(r.data[0]["data"]).get("prompt")
            if p:
                _qbm_prompt_cache["prompt"] = p
                return p
    except Exception as e:
        logger.warning(f"[QBM] prompt memory load failed: {e}")
    _qbm_prompt_cache["prompt"] = QBM_EXTRACT_PROMPT_DEFAULT
    return QBM_EXTRACT_PROMPT_DEFAULT

def qbm_set_active_prompt(new_prompt: str):
    """New prompt update এলে সেটাকে permanent করে save করে — পরের বার থেকে
    (নতুন update না আসা অবধি) এই prompt-ই সবসময় ব্যবহার হবে।"""
    _qbm_prompt_cache["prompt"] = new_prompt
    try:
        sb.table("quiz_sessions").upsert({
            "key": "qbm_active_prompt",
            "data": json.dumps({"prompt": new_prompt}),
            "updated_at": int(time.time())
        }).execute()
    except Exception as e:
        logger.warning(f"[QBM] prompt memory save failed: {e}")


def _has_mixed_digit_script(text: str) -> bool:
    """একই সংখ্যা token-এ Bengali+English digit মিশে থাকলে সেটা corruption সংকেত।"""
    if not text:
        return False
    bn_digits = set('০১২৩৪৫৬৭৮৯')
    for token in re.findall(r'[০-৯0-9]+', text):
        has_bn = any(c in bn_digits for c in token)
        has_en = any(c.isdigit() and c not in bn_digits for c in token)
        if has_bn and has_en:
            return True
    return False


def _qbm_parse_json(text: str) -> list:
    """Parse extractor JSON output -> list of {question, options[A-D], answer(A-D), explanation}"""
    if not text:
        return []
    t = text.strip()
    if "```json" in t:
        t = t.split("```json")[1].split("```")[0].strip()
    elif "```" in t:
        t = t.split("```")[1].split("```")[0].strip()
    try:
        m = re.search(r'\[.*\]', t, re.DOTALL)
        raw = json.loads(m.group()) if m else json.loads(t)
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    valid = []
    for mc in raw:
        try:
            q = mc.get("question", "")
            opts = mc.get("options", {})
            if not q or not opts:
                continue
            # Clean numbering prefix + trailing bracket artifacts
            q = re.sub(r'\s*[\[\(].*?[\]\)]\s*$', '', q)
            q = _strip_q_numbering(q)
            opts_list = [opts.get("A", ""), opts.get("B", ""), opts.get("C", ""), opts.get("D", "")]
            expl = mc.get("explanation", "")
            if _has_mixed_digit_script(q) or any(_has_mixed_digit_script(o) for o in opts_list) or _has_mixed_digit_script(expl):
                logger.warning(f"[QBM digit-integrity] Mixed Bengali/English digits detected: {q[:60]}")
            valid.append({
                "question": q.strip(),
                "options": opts_list,
                "answer": mc.get("answer", "A") if mc.get("answer") in ("A", "B", "C", "D") else "A",
                "explanation": expl
            })
        except Exception:
            continue
    return valid


async def _qbm_groq_call(img, prompt: str) -> str:
    """Raw Groq call helper — returns raw text (caller parses)."""
    keys = groq_key_rotator.all_keys()
    if not keys:
        return ""
    data_url = _img_to_data_url(img)
    if not data_url:
        return ""
    for key in keys:
        txt, status = await _post_openai_compat(
            "https://api.groq.com/openai/v1/chat/completions",
            key, "qwen/qwen3.6-27b",
            data_url, prompt
        )
        if txt:
            return txt
        if status != 429:
            logger.warning(f"[Groq-QBM] key failed (status={status}), trying next key")
    return ""


async def _qbm_call1_extract(img) -> list:
    """
    CALL 1 — OWN OCR + strict-prompt MCQ extraction + inline dedup.
    Job: extract every existing MCQ on the page (option-serial strictly
    preserved per active prompt), while checking-as-it-goes so no duplicate
    /ghost MCQ enters the list. Groq primary -> Gemini fallback.
    """
    try:
        prompt = qbm_get_active_prompt()
        txt = await _qbm_groq_call(img, prompt)
        result = _qbm_parse_json(txt) if txt else []
        if result:
            return _qbm_dedup_list(result)
        gem = await _qbm_gemini_extract(img, prompt)
        return _qbm_dedup_list(gem)
    except Exception as e:
        logger.warning(f"[QBM Call1] failed: {e}")
        return []


async def _qbm_call2_miss_check(img, call1_mcqs: list) -> list:
    """
    CALL 2 — MAIN JOB: verify Call-1 caught every existing MCQ on the page;
    if any were missed, add them (never remove valid ones). Then re-runs a
    fast duplicate/ghost-MCQ check on the combined list.
    Connected to Call-1: audits Call-1's specific output rather than
    re-extracting independently from scratch.
    """
    try:
        q_summary = "\n".join(
            f"{i+1}. {(m.get('question') or '')[:100]}" for i, m in enumerate(call1_mcqs)
        )
        prompt = f"""You already extracted these MCQs from this exact page image (Call 1 result):
{q_summary if q_summary else "(none found)"}

TASK (fast audit, connected to Call 1 — do not redo full extraction):
1) MANDATORY: mentally divide the page into regions (top/middle/bottom, left/right if
   multi-column) and check EACH region against the list above before answering —
   especially the LAST MCQ on the page and the BOTTOM of the page — most commonly missed.
2) Check if ANY existing MCQ was MISSED by the list above.
3) If you find missed MCQ(s), extract them in the SAME strict format (options in the exact
   source position order, A/B/C/D slots by position — never relabeled/sorted).
4) UDDIPOK CHECK: if a missed MCQ belongs under a passage/উদ্দীপক, prepend that passage's full
   text to its question (self-contained), same as Call 1's rule.
5) Do NOT re-list MCQs already shown above. Only output NEW ones that were missed.
6) If nothing was missed, output exactly: []

Output ONLY a JSON array of the MISSED MCQs (same schema as before):
[{{"question":"...","options":{{"A":"...","B":"...","C":"...","D":"..."}},"answer":"A/B/C/D","explanation":"..."}}]"""
        txt = await _qbm_groq_call(img, prompt)
        missed = _qbm_parse_json(txt) if txt else []
        if not missed:
            gem_txt = await _qbm_gemini_raw(img, prompt)
            missed = _qbm_parse_json(gem_txt) if gem_txt else []

        combined = list(call1_mcqs) + missed
        # 2nd dedup pass (fast, since Call-1 already deduped once) — catches any
        # duplicate/ghost MCQ that slipped in via the miss-check addition.
        return _qbm_dedup_list(combined)
    except Exception as e:
        logger.warning(f"[QBM Call2] failed: {e}")
        return call1_mcqs


async def _qbm_final_empty_page_scan(img) -> list:
    """
    CALL 3 (empty-page variant) — used ONLY when Call 1 AND Call 2 BOTH
    returned zero MCQs. Two independent empty results is a strong signal but
    not proof (both calls could share the same blind spot, e.g. faint text,
    an MCQ tucked in a corner/footnote, or an OCR gap). This does one final,
    independent, fresh-eyes scan of the raw page image before the pipeline is
    allowed to confirm "this page truly has 0 MCQ".
    Strong combination rule: only when ALL THREE calls (1, 2, and this final
    scan) agree on zero is the page marked confirmed-empty.
    """
    try:
        prompt = """Two prior independent passes over this exact page image both concluded
there is NO existing MCQ (multiple-choice question) on this page.

TASK: Do one final, completely fresh scan of the page, ignoring the prior
passes' conclusion. Look carefully at every part of the page including
footnotes, page corners/margins, faint or small text, and any question that
might be split across a passage/উদ্দীপক.

If you find even ONE existing MCQ that was missed, extract it in the strict
format below (options in exact source position order, A/B/C/D by position —
never relabeled/sorted). If it depends on a passage/উদ্দীপক, prepend that
passage's full text to the question (self-contained).

If the page genuinely has no MCQ at all, output exactly: []

Output ONLY a JSON array:
[{"question":"...","options":{"A":"...","B":"...","C":"...","D":"..."},"answer":"A/B/C/D","explanation":"..."}]"""
        txt = await _qbm_groq_call(img, prompt)
        found = _qbm_parse_json(txt) if txt else []
        if not found:
            gem_txt = await _qbm_gemini_raw(img, prompt)
            found = _qbm_parse_json(gem_txt) if gem_txt else []
        return _qbm_dedup_list(found) if found else []
    except Exception as e:
        logger.warning(f"[QBM Call3-empty] failed: {e}")
        return []


async def _qbm_call3_verify(img, mcqs: list, page_confirmed_complete: bool) -> list:
    """
    CALL 3 — per-MCQ verification, connected to Call 1+2:
    - If Call 1 & 2 already agree the page/MCQ set is 100% confirmed (no misses,
      no duplicates), this call SKIPS the heavy re-extraction and only does one
      fast recheck pass — it does not redundantly re-verify from scratch.
    - Confirms for each MCQ: answer letter matches the correct option's actual
      output position (option-serial integrity), the answer itself matches what
      the source page shows, and no spelling mistakes (Bangla/English) remain.
    - If any MCQ's answer is unclear from the page, tries twice to reason it out;
      if still unclear, picks the AI's best answer (last resort) and builds the
      explanation from that chosen answer.
    """
    if not mcqs:
        return mcqs
    try:
        mcq_json = json.dumps([
            {"question": m.get("question", ""), "options": m.get("options", []),
             "answer": m.get("answer", "A"), "explanation": m.get("explanation", "")}
            for m in mcqs
        ], ensure_ascii=False)

        mode_note = (
            "Call 1 and Call 2 already fully confirmed this page (no misses, no "
            "duplicates) — so do ONE FAST recheck pass only, do not over-analyze."
            if page_confirmed_complete else
            "Do a careful full verification pass on every MCQ below."
        )

        prompt = f"""{mode_note}

Here is the current MCQ list extracted from this page image (Call 1 + Call 2 combined):
{mcq_json}

VERIFY each MCQ against the actual page image, in this exact order of checks:
1) OPTION-SERIAL INTEGRITY: is the answer letter (A/B/C/D) pointing to the option that is
   actually in that position in THIS output list (not the source's original label)? If the
   correct option's text sits in position 2 of the output, answer must be "B", etc. Fix any
   mismatch.
2) ANSWER SOURCE MATCH: does the answer actually match what is marked/given on the page
   (marked option, inline answer, bottom-of-page answer box/table, or answer key found on
   another page — scan forward/backward through all pages as needed)? Fix if wrong.
3) If an MCQ's answer is genuinely unclear from any source → try twice, reasoning it out from
   context, to determine the most likely correct answer. If STILL unclear after 2 tries, choose
   your own best answer, and base the explanation on that chosen answer.
4) SPELLING CHECK: check question + all options + explanation for spelling mistakes (Bangla or
   English) and correct them, without changing meaning.
5) Re-confirm option order was never reshuffled and math/chemistry sub/superscripts (H₂O, x²,
   Na⁺ etc.) are correctly rendered everywhere.
6) UDDIPOK CHECK: for any MCQ that depends on a passage/উদ্দীপক, confirm its full passage text
   is prepended to the question (self-contained). Fix/add if missing.

Output ONLY the corrected full JSON array (same length as input, same schema, all fixes applied):
[{{"question":"...","options":{{"A":"...","B":"...","C":"...","D":"..."}},"answer":"A/B/C/D","explanation":"..."}}]"""

        txt = await _qbm_groq_call(img, prompt)
        verified = _qbm_parse_json(txt) if txt else []
        if verified and len(verified) >= len(mcqs) * 0.8:
            return _cap_mcq_options(verified)
        return mcqs  # verify failed/degraded -> keep Call1+2 result, never lose data
    except Exception as e:
        logger.warning(f"[QBM Call3] failed: {e}")
        return mcqs


def _qbm_normalize_q(question: str) -> str:
    """Whitespace/punctuation normalize করে দুইটা pass-এর একই MCQ-কে duplicate ধরার জন্য."""
    q = re.sub(r'\s+', ' ', (question or '').strip().lower())
    q = re.sub(r'[^\w\u0980-\u09FF ]+', '', q)
    return q


def _qbm_is_duplicate(norm_q: str, existing_keys: list, threshold: float = 0.85) -> bool:
    """Exact match না থাকলেও near-identical প্রশ্ন (pass ভেদে সামান্য spelling/space
    difference) কে duplicate হিসেবে ধরার জন্য fuzzy match।"""
    if not norm_q:
        return True
    if norm_q in existing_keys:
        return True
    for k in existing_keys:
        if not k:
            continue
        shorter, longer = (k, norm_q) if len(k) <= len(norm_q) else (norm_q, k)
        if shorter and shorter in longer and len(shorter) >= 0.7 * len(longer):
            return True
        if difflib.SequenceMatcher(None, norm_q, k).ratio() >= threshold:
            return True
    return False


def _qbm_dedup_list(mcqs: list) -> list:
    """Fuzzy-dedup a list in place order, dropping duplicate/ghost MCQs."""
    seen_keys: list = []
    out = []
    for mc in mcqs:
        key_q = _qbm_normalize_q(mc.get("question", ""))
        if not key_q:
            continue
        if not _qbm_is_duplicate(key_q, seen_keys):
            seen_keys.append(key_q)
            out.append(mc)
    return out


# v-RAM-fix: caps how many pages (across ALL users) run the 3-call extraction
# pipeline at once. Each in-flight page holds a decoded PIL image + growing
# MCQ list in RAM; on a 512MB free instance, many users uploading at the same
# time without this cap could still spike RAM even with the PDF-convert lock
# (that lock only guards the pdf2image step, not the extraction step after).
# v-RAM-fix: dynamic RAM-aware gate instead of a fixed slot count. Each
# in-flight page holds a decoded image + growing MCQ list; under heavy
# concurrent load (many users at once) a FIXED cap either wastes headroom
# (too low) or risks OOM (too high). This checks live RSS before admitting
# a new page into the pipeline -- lets many run in parallel while RAM is
# free, throttles automatically as it fills, protecting against a 100-user
# spike without hardcoding a number that's wrong for either extreme.
_QBM_EXTRACT_HARD_CAP = asyncio.Semaphore(150)  # was 20 on 512MB Render; raised for 16GB HF Space
_qbm_ram_gate_lock = asyncio.Lock()

async def _qbm_ram_aware_acquire():
    """Blocks until (a) a hard-cap slot is free AND (b) live RSS has headroom."""
    await _QBM_EXTRACT_HARD_CAP.acquire()
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        limit_mb = 14000  # 16GB instance ceiling, matches _ram_guard_task
        safe_ceiling_mb = int(limit_mb * 0.75)  # leave margin under the 85% RAMGuard threshold
        while True:
            rss_mb = proc.memory_info().rss / (1024 * 1024)
            if rss_mb < safe_ceiling_mb:
                return
            await asyncio.sleep(0.5)
    except ImportError:
        return  # psutil unavailable -> fall back to hard cap only


async def _qbm_extract_from_image(img) -> list:
    """
    3-CALL CONNECTED PIPELINE (per page), replacing the old independent-pass
    system. Each call has one distinct job and is connected to the others —
    a call that already confirms something tells the next call to skip
    redundant re-work instead of re-verifying everything from scratch.

    Call 1 (extract): own-OCR + strict prompt MCQ extraction, inline dedup.
    Call 2 (miss-check, MAIN job): confirms Call 1 caught every MCQ on the
        page; adds any missed one; fast re-dedup on the combined list.
    Call 3 (verify): if Call 1+2 found zero misses and zero duplicates, this
        page is "confirmed complete" -> Call 3 only does one fast recheck
        pass (option-serial + answer-source + spelling). Otherwise it does a
        careful full verification pass.
    Never fabricates new questions — only extracts/fixes what already exists.
    """
    await _qbm_ram_aware_acquire()
    try:
        call1 = await _qbm_call1_extract(img)

        # Even if Call 1 found nothing, Call 2 still re-checks the page for a
        # missed MCQ before we declare it genuinely empty -- a single failed/
        # empty Call 1 (e.g. transient parse issue) should never silently zero
        # out a page that Call 2 could still catch.
        before_call2 = len(call1)
        call2 = await _qbm_call2_miss_check(img, call1)
        page_confirmed_complete = (len(call2) == before_call2)  # no misses added, no dupes removed

        if not call2:
            # STRONG COMBINATION CHECK: Call 1 and Call 2 both say zero. Don't
            # trust two-in-agreement alone -- run one final independent scan
            # (Call 3's empty-page variant). Only if all THREE calls agree on
            # zero is the page confirmed truly empty.
            final_check = await _qbm_final_empty_page_scan(img)
            if not final_check:
                return []  # Confirmed by all 3 calls: page genuinely has no MCQ
            # Call 3 found something the first two missed -- verify it properly.
            return _cap_mcq_options(await _qbm_call3_verify(img, final_check, False))

        call3 = await _qbm_call3_verify(img, call2, page_confirmed_complete)
        return _cap_mcq_options(call3)
    finally:
        _QBM_EXTRACT_HARD_CAP.release()


async def _qbm_gemini_raw(img, prompt: str) -> str:
    """Direct Gemini call with any given prompt -> raw text (caller parses)."""
    try:
        from pdf_handler import key_rotator, image_to_base64
        if not key_rotator.keys:
            return ""
        key = key_rotator.get_key()
        from google import genai as gai
        from google.genai import types
        client = gai.Client(api_key=key)
        img_b64 = image_to_base64(img)

        def _call():
            return client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    types.Part.from_text(text=prompt),
                    types.Part.from_bytes(data=base64.b64decode(img_b64), mime_type="image/jpeg")
                ],
                config=types.GenerateContentConfig(temperature=0.1)
            )
        response = await asyncio.to_thread(_call)
        return response.text or ""
    except Exception as e:
        logger.warning(f"[QBM] Gemini raw call failed: {e}")
        return ""


async def _qbm_gemini_extract(img, prompt: str = None) -> list:
    """Direct Gemini call with the strict extraction prompt (fallback path)."""
    txt = await _qbm_gemini_raw(img, prompt or qbm_get_active_prompt())
    return _qbm_parse_json(txt) if txt else []


async def handle_qbm(msg: dict):
    """
    /qbm -p (pages) -c (channel) -m (topic) -t (thread_id)
    PDF-এ থাকা EXISTING MCQ extract করে (নতুন MCQ বানায় না)।
    """
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]
    lock = _get_pdfm_lock(uid)
    if lock.locked():
        _PDFM_USER_QUEUE_LEN[uid] = _PDFM_USER_QUEUE_LEN.get(uid, 0) + 1
        pos = _PDFM_USER_QUEUE_LEN[uid]
        try:
            await send_msg(chat_id, f"⏳ আগের PDF/PPT কাজ শেষ হচ্ছে... তোমার এই request queue তে #{pos} নম্বরে আছে, একে একে সব হয়ে যাবে।")
        except Exception:
            pass
    async with lock:
        _PDFM_USER_QUEUE_LEN[uid] = max(0, _PDFM_USER_QUEUE_LEN.get(uid, 1) - 1)
        return await _handle_qbm_impl(msg)


async def _handle_qbm_impl(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    uname = msg["from"].get("first_name", "User")
    text = msg.get("text", "")
    reply = msg.get("reply_to_message")

    if not reply or not (reply.get("document") or reply.get("photo")):
        await send_msg(chat_id,
            "❌ PDF বা Image-এ reply করে /qbm দাও!\n\n"
            "<b>Format:</b>\n"
            "<code>/qbm -p 1-5 -c @channel -m \"Topic\" -t group_id</code>\n\n"
            "📌 এই ফিচার PDF/Image-এ আগে থেকে থাকা MCQ extract করে (নতুন বানায় না)\n"
            "📌 -p = page range, PDF-only (না দিলে সব page)\n"
            "📌 -c = channel id (না দিলে list দেখাবে)\n"
            "📌 -m = topic name\n"
            "📌 -t = topic/thread id (group হলে)"
        )
        return

    is_image_reply = bool(reply.get("photo")) or (
        reply.get("document") and not reply["document"].get("file_name", "").lower().endswith(".pdf")
        and (reply["document"].get("mime_type", "").startswith("image/"))
    )

    if reply.get("document") and not is_image_reply and not reply["document"].get("file_name", "").lower().endswith(".pdf"):
        await send_msg(chat_id, "❌ শুধু PDF বা Image file support করে!")
        return

    params = _parse_pdfm_params(text)
    topic = params["topic"] or "🌟ATLAS Question Bank"
    page_range = params["page_range"]
    channel_id = params["channel_id"]
    thread_id = params["thread_id"]

    if is_image_reply:
        if reply.get("photo"):
            file_id = reply["photo"][-1]["file_id"]
        else:
            file_id = reply["document"]["file_id"]
        file_name = reply.get("document", {}).get("file_name", "image.jpg")
    else:
        file_id = reply["document"]["file_id"]
        file_name = reply["document"].get("file_name", "document.pdf")

    status_r = await send_msg(chat_id, "⏳ " + ("Image" if is_image_reply else "PDF") + " download হচ্ছে...")
    status_msg_id = status_r.get("result", {}).get("message_id")

    try:
        if is_image_reply:
            img_bytes = await download_tg_file(file_id, chat_id=chat_id, message_id=reply["message_id"])
            from PIL import Image as PILImage
            img = PILImage.open(BytesIO(img_bytes))
            pages = [(1, img)]
        else:
            pdf_bytes = await _download_pdf_cached(file_id, chat_id=chat_id, message_id=reply["message_id"])
            ok, pages = await asyncio.to_thread(_render_pdf_cached, file_id, pdf_bytes, page_range)
            if not ok:
                await send_msg(chat_id, pages)
                return

            # OCR fallback if truly no pages/images came back (scanned/corrupt PDF).
            # NOTE: previous check `len(str(pages[0][1])) < 100` was always true (PIL repr
            # string is always short) so this branch fired on every PDF and ignored
            # page_range — fixed to only trigger on genuine empty extraction, and to
            # respect page_range when it does.
            if not pages:
                if status_msg_id:
                    await edit_msg(chat_id, status_msg_id, "🔍 OCR Scanning (scanned PDF detected)...")
                try:
                    from pdf2image import convert_from_bytes
                    if page_range:
                        parts = str(page_range).split("-")
                        first = int(parts[0])
                        last = int(parts[1]) if len(parts) > 1 else first
                        ocr_images = await asyncio.to_thread(
                            convert_from_bytes, pdf_bytes, dpi=150, first_page=first, last_page=last
                        )
                        pages = list(zip(range(first, last + 1), ocr_images))
                    else:
                        ocr_images = await asyncio.to_thread(convert_from_bytes, pdf_bytes, dpi=150)
                        pages = list(enumerate(ocr_images, 1))
                except Exception as e:
                    logger.warning(f"[QBM] OCR fallback failed: {e}")

        if not pages:
            if status_msg_id:
                await edit_msg(chat_id, status_msg_id, "❌ Page পাওয়া যায়নি!")
            return

        if status_msg_id:
            await edit_msg(chat_id, status_msg_id,
                f"✅ {len(pages)} page পাওয়া গেছে!\n⏳ MCQ Extraction শুরু হচ্ছে...")

        # ── MCQ Extraction ALWAYS runs first (3-call pipeline, per page) ──
        # Channel selection + CSV file generation happen only AFTER extraction
        # is fully complete, so the person picks a channel already knowing
        # exactly how many MCQs were found.
        extracted_pages = await qbm_extract_all_pages(
            chat_id, pages, topic, file_name, status_msg_id
        )

        if not channel_id:
            channels = await db_get_channels()
            if not channels:
                await process_qbm_pages(chat_id, uid, uname, extracted_pages, topic,
                    channel_id, True, file_name, status_msg_id, thread_id, skip_extract=True)
                return

            app.state.qbm_cache = getattr(app.state, "qbm_cache", {})
            app.state.qbm_cache[f"qbm_img_{uid}"] = extracted_pages
            _cap_page_cache(app.state.qbm_cache)
            sb.table("quiz_sessions").upsert({
                "key": f"qbm_pending_{uid}",
                "data": json.dumps({
                    "topic": topic, "file_name": file_name,
                    "status_msg_id": status_msg_id, "thread_id": thread_id,
                    "file_id": file_id, "page_range": page_range
                }),
                "updated_at": int(time.time())
            }).execute()

            total_mcq_found = sum(len(mcqs) for _, _, mcqs in extracted_pages)
            page_breakdown = "\n".join(
                f"📌 Page {fmt_page(p)}: {len(mcqs)} MCQ" for p, _, mcqs in extracted_pages
            )
            kb = {"inline_keyboard": []}
            for ch in channels:
                ch_id = ch.get("channel_id", "")
                ch_name = ch.get("channel_name", ch_id)
                kb["inline_keyboard"].append([{
                    "text": f"📢 {ch_name}",
                    "callback_data": f"qbmch_{ch_id}_{uid}"
                }])
            kb["inline_keyboard"].append([{
                "text": "📄 CSV Only",
                "callback_data": f"qbmch_csv_{uid}"
            }])
            await send_msg(chat_id,
                f"✅ Extraction Complete! {total_mcq_found} MCQ পাওয়া গেছে ({len(pages)} page)\n\n"
                f"{page_breakdown}\n\n"
                f"🎯 Topic: {topic}\n\nChannel select করো:",
                reply_markup=kb
            )
            return

        await process_qbm_pages(chat_id, uid, uname, extracted_pages, topic,
            channel_id, False, file_name, status_msg_id, thread_id, skip_extract=True)

    except Exception as e:
        logger.error(f"[QBM] Error: {e}", exc_info=True)
        await _safe_error_reply(chat_id, e)


async def _qbm_scan_answer_key(img, unresolved_mcqs: list) -> dict:
    """
    Given a page image and a list of MCQs whose answer wasn't found on their
    own page, check if THIS page contains an answer key (table, boxed list,
    or "1-A, 2-C..." style) that matches any of these questions by text.
    Returns {question_text_first_80_chars: answer_letter} for matches found.
    Never guesses — only returns a match if the page genuinely contains one.
    """
    if not unresolved_mcqs:
        return {}
    try:
        q_list = "\n".join(
            f"{i+1}. {(m.get('question') or '').strip()[:150]}"
            for i, m in enumerate(unresolved_mcqs)
        )
        prompt = f"""This image may contain an ANSWER KEY (a table, boxed list, or a line
like "1-A, 2-C, 3-B..." mapping question numbers to correct options).

Here are questions whose answers are still missing, in order:
{q_list}

Task: If this page contains an answer key that matches ANY of these questions
(by matching question number sequence, or by recognizing the question topic),
return a JSON array like:
[{{"question_index": 1, "answer": "A"}}, {{"question_index": 3, "answer": "C"}}]

Only include entries where you found a genuine, confident match on this page.
If this page has no answer key at all, or no match for these specific questions,
return exactly: []
Return ONLY the JSON array, nothing else."""

        keys = groq_key_rotator.all_keys()
        result_json = None
        if keys:
            data_url = _img_to_data_url(img)
            if data_url:
                for key in keys:
                    txt, status = await _post_openai_compat(
                        "https://api.groq.com/openai/v1/chat/completions",
                        key, "qwen/qwen3.6-27b",
                        data_url, prompt
                    )
                    if txt:
                        result_json = _qbm_parse_json(txt)
                        break
                    if status != 429:
                        logger.warning(f"[Groq-QBM2] key failed (status={status}), falling through to Gemini")
                        break

        if not result_json:
            try:
                from pdf_handler import key_rotator, image_to_base64
                if key_rotator.keys:
                    gkey = key_rotator.get_key()
                    from google import genai as gai
                    from google.genai import types
                    client = gai.Client(api_key=gkey)
                    img_b64 = image_to_base64(img)

                    def _call():
                        return client.models.generate_content(
                            model="gemini-2.5-flash",
                            contents=[
                                types.Part.from_text(text=prompt),
                                types.Part.from_bytes(data=base64.b64decode(img_b64), mime_type="image/jpeg")
                            ]
                        )
                    response = await asyncio.to_thread(_call)
                    result_json = _qbm_parse_json(response.text)
            except Exception as e:
                logger.warning(f"[QBM] answer-key scan gemini fallback failed: {e}")

        if not result_json or not isinstance(result_json, list):
            return {}

        found = {}
        for entry in result_json:
            try:
                q_idx = int(entry.get("question_index", 0)) - 1
                ans = str(entry.get("answer", "")).strip().upper()[:1]
                if 0 <= q_idx < len(unresolved_mcqs) and ans in ("A", "B", "C", "D"):
                    key_text = (unresolved_mcqs[q_idx].get("question") or "").strip()[:80]
                    found[key_text] = ans
            except (ValueError, TypeError, AttributeError):
                continue
        return found
    except Exception as e:
        logger.warning(f"[QBM] answer-key scan failed: {e}")
        return {}


async def qbm_extract_all_pages(
    chat_id: int, pages: list, topic: str,
    file_name: str, status_msg_id: int = None
) -> list:
    """
    Phase 1 -- runs the full 3-call connected extraction pipeline for every
    page BEFORE any channel selection or posting happens. Also performs the
    cross-page answer backfill lookahead here (same as before), so by the
    time this returns, every page's MCQ list is fully final.
    Returns list of (page_num, img, mcqs) tuples.
    """
    page_status = [{"page": p, "done": False, "current": False, "mcq": 0} for p, _ in pages]
    start_time = time.time()
    total_mcq = 0
    results = [None] * len(pages)
    set_active_job(chat_id, f"QBM extraction ({file_name}, page-by-page)")

    if status_msg_id:
        await edit_msg(chat_id, status_msg_id,
            _build_dashboard(file_name, topic, pages, page_status, start_time, 0, 0))

    async def _extract_one(idx, page_num, img):
        if is_cancelled(chat_id):
            return idx, page_num, img, []
        page_status[idx]["current"] = True
        mcqs = []
        try:
            mcqs = await _qbm_extract_from_image(img)

            unresolved = [m for m in mcqs if "Answer not found in source" in (m.get("explanation") or "")]
            if unresolved and idx + 1 < len(pages):
                for lookahead_offset in (1, 2):
                    if idx + lookahead_offset >= len(pages):
                        break
                    if not unresolved:
                        break
                    _, lookahead_img = pages[idx + lookahead_offset]
                    found_map = await _qbm_scan_answer_key(lookahead_img, unresolved)
                    if found_map:
                        for m in mcqs:
                            key = (m.get("question") or "").strip()[:80]
                            if key in found_map:
                                m["answer"] = found_map[key]
                                m["explanation"] = (m.get("explanation") or "").replace(
                                    "Answer not found in source",
                                    f"Answer key p.{fmt_page(pages[idx + lookahead_offset][0])} থেকে matched"
                                )
                        unresolved = [m for m in mcqs if "Answer not found in source" in (m.get("explanation") or "")]
        except Exception as e:
            logger.error(f"[QBM Extract] Page {page_num} error: {e}")
        return idx, page_num, img, mcqs

    # Windowed concurrency: extract several pages in parallel instead of one at
    # a time -- the RAM-aware semaphore (_QBM_EXTRACT_HARD_CAP) still throttles
    # actual concurrent Gemini calls, this just stops needlessly serializing
    # pages that don't depend on each other's extraction result.
    WINDOW = 5
    for start in range(0, len(pages), WINDOW):
        if is_cancelled(chat_id):
            break
        chunk = pages[start:start + WINDOW]
        tasks = [
            _extract_one(start + i, page_num, img)
            for i, (page_num, img) in enumerate(chunk)
        ]
        chunk_results = await asyncio.gather(*tasks)
        for idx, page_num, img, mcqs in chunk_results:
            results[idx] = (page_num, img, mcqs)
            total_mcq += len(mcqs)
            page_status[idx]["current"] = False
            page_status[idx]["done"] = True
            page_status[idx]["mcq"] = len(mcqs)
        if status_msg_id:
            await edit_msg(chat_id, status_msg_id,
                _build_dashboard(file_name, topic, pages, page_status, start_time, total_mcq, 0))

    clear_active_job(chat_id)
    return [r for r in results if r is not None]


async def process_qbm_pages(
    chat_id: int, uid: int, uname: str,
    pages: list, topic: str,
    channel_id, csv_only: bool,
    file_name: str = "document.pdf",
    status_msg_id: int = None,
    thread_id: int = None,
    skip_extract: bool = False
):
    """
    QBM posting loop. If skip_extract=True, `pages` is already a list of
    (page_num, img, mcqs) tuples from qbm_extract_all_pages() -- extraction
    (and the 3-call pipeline + answer backfill) already happened in Phase 1,
    so this function only posts to Telegram / builds the CSV.
    """
    settings = await db_get_settings()
    tag = settings.get("tag", "")
    exp_footer = settings.get("exp_footer", "")

    if skip_extract:
        page_tuples = pages  # (page_num, img, mcqs)
        display_pages = [(p, img) for p, img, _ in page_tuples]
    else:
        page_tuples = None
        display_pages = pages

    page_status = [{"page": p, "done": False, "current": False, "mcq": 0} for p, _ in display_pages]
    start_time = time.time()
    total_mcq = 0
    total_polls = 0

    if not status_msg_id:
        r = await send_msg(chat_id, "⏳ Posting শুরু হচ্ছে...")
        status_msg_id = r.get("result", {}).get("message_id")

    await edit_msg(chat_id, status_msg_id,
        _build_dashboard(file_name, topic, display_pages, page_status, start_time, 0, 0))

    summary_pages = []
    all_mcqs_csv = []
    first_image_msg_id = None
    set_active_job(chat_id, f"QBM Poll posting ({file_name}, page-by-page)")

    iterable = page_tuples if skip_extract else [(p, img, None) for p, img in pages]

    for idx, (page_num, img, precomputed_mcqs) in enumerate(iterable):
        if is_cancelled(chat_id):
            clear_active_job(chat_id)
            break
        page_status[idx]["current"] = True
        await edit_msg(chat_id, status_msg_id,
            _build_dashboard(file_name, topic, display_pages, page_status, start_time, total_mcq, total_polls))

        try:
            mcqs = precomputed_mcqs if skip_extract else await _qbm_extract_from_image(img)
            if not mcqs:
                page_status[idx]["current"] = False
                page_status[idx]["done"] = True
                await edit_msg(chat_id, status_msg_id,
                    _build_dashboard(file_name, topic, display_pages, page_status, start_time, total_mcq, total_polls))
                continue

            # ── Cross-page answer backfill — SKIPPED here if skip_extract=True
            # (Phase 1 / qbm_extract_all_pages already ran this lookahead) ──
            if not skip_extract:
                unresolved = [m for m in mcqs if "Answer not found in source" in (m.get("explanation") or "")]
                if unresolved and idx + 1 < len(display_pages):
                    for lookahead_offset in (1, 2):
                        if idx + lookahead_offset >= len(display_pages):
                            break
                        if not unresolved:
                            break
                        _, lookahead_img = display_pages[idx + lookahead_offset]
                        found_map = await _qbm_scan_answer_key(lookahead_img, unresolved)
                        if found_map:
                            for m in mcqs:
                                key = (m.get("question") or "").strip()[:80]
                                if key in found_map:
                                    m["answer"] = found_map[key]
                                    m["explanation"] = (m.get("explanation") or "").replace(
                                        "Answer not found in source",
                                        f"Answer key p.{fmt_page(display_pages[idx + lookahead_offset][0])} থেকে matched"
                                    )
                            unresolved = [m for m in mcqs if "Answer not found in source" in (m.get("explanation") or "")]

            img_bytes = image_to_bytes(img) if not isinstance(img, (bytes, bytearray)) else img

            if csv_only:
                for m in mcqs:
                    opts = m.get("options", ["", "", "", ""])
                    ans_map = {"A": "1", "B": "2", "C": "3", "D": "4"}
                    ans_num = ans_map.get(m.get("answer", "A"), "1")
                    all_mcqs_csv.append([m["question"], opts[0],
                        opts[1] if len(opts) > 1 else "",
                        opts[2] if len(opts) > 2 else "",
                        opts[3] if len(opts) > 3 else "",
                        ans_num, _strip_img_tag(m.get("explanation", "")), "1", "1"])
            else:
                caption = ""
                if tag:
                    caption = f"{tag}\n\n"
                caption += (
                    f"📋ATLAS Question Bank Extraction\n"
                    f"🌟Topic: {topic}\n"
                    f"📌Page No: {fmt_page(page_num)}\n"
                    f"💎MCQ: {len(mcqs)}"
                )

                photo_r = await send_photo(channel_id, img_bytes, caption, message_thread_id=thread_id)
                image_msg_id = None
                if photo_r.get("ok"):
                    image_msg_id = photo_r["result"]["message_id"]
                    if first_image_msg_id is None:
                        first_image_msg_id = image_msg_id
                        # Item 3: auto-pin the very first image of the job
                        await try_pin_message(channel_id, image_msg_id)

                first_poll_link = ""
                for i, mcq in enumerate(mcqs):
                    opts = mcq.get("options", [])[:4]
                    ans_idx = {"A": 0, "B": 1, "C": 2, "D": 3}.get(mcq.get("answer", "A"), 0)
                    q_text = mcq["question"]
                    if tag:
                        q_text = f"{tag}\n\n{q_text}"
                    exp = mcq.get("explanation", "")
                    if exp_footer:
                        exp = f"{exp}\n{exp_footer}"
                    poll_r = {"ok": False}
                    for _attempt in range(3):
                        poll_r = await send_poll(
                            channel_id, q_text, opts, ans_idx,
                            explanation=exp,
                            reply_to_message_id=image_msg_id,
                            message_thread_id=thread_id
                        )
                        if poll_r.get("ok"):
                            break
                        await asyncio.sleep(2)
                    if poll_r.get("ok") and i == 0:
                        pmid = poll_r["result"]["message_id"]
                        cid = str(channel_id)
                        first_poll_link = (
                            f"https://t.me/c/{cid[4:]}/{pmid}"
                            if cid.startswith("-100")
                            else f"https://t.me/{cid.lstrip('@')}/{pmid}"
                        )
                    total_polls += 1
                    await asyncio.sleep(1.0)

                end_text = (
                    f"🚀Topic: {topic}\n"
                    f"🌟Page No: {fmt_page(page_num)}\n"
                    f"✅MCQ: {len(mcqs)}\n"
                    f"🔗First Poll Link:\n{first_poll_link}"
                )
                end_data = {
                    "chat_id": channel_id, "text": end_text,
                    "reply_to_message_id": image_msg_id,
                    "disable_web_page_preview": True
                }
                if thread_id:
                    end_data["message_thread_id"] = thread_id
                await tg_post("sendMessage", end_data)

                summary_pages.append({"page": page_num, "first_poll": first_poll_link, "mcq_count": len(mcqs)})

                for m in mcqs:
                    opts = m.get("options", ["", "", "", ""])
                    ans_map = {"A": "1", "B": "2", "C": "3", "D": "4"}
                    ans_num = ans_map.get(m.get("answer", "A"), "1")
                    all_mcqs_csv.append([m["question"], opts[0],
                        opts[1] if len(opts) > 1 else "",
                        opts[2] if len(opts) > 2 else "",
                        opts[3] if len(opts) > 3 else "",
                        ans_num, _strip_img_tag(m.get("explanation", "")), "1", "1"])

            total_mcq += len(mcqs)
            page_status[idx]["done"] = True
            page_status[idx]["current"] = False
            page_status[idx]["mcq"] = len(mcqs)
            await edit_msg(chat_id, status_msg_id,
                _build_dashboard(file_name, topic, display_pages, page_status, start_time, total_mcq, total_polls))

        except Exception as e:
            logger.error(f"[QBM] Page {page_num} error: {e}")
            page_status[idx]["current"] = False
            page_status[idx]["done"] = True

    clear_active_job(chat_id)

    if all_mcqs_csv:
        import io as _io, csv as _csv_mod
        buf = _io.StringIO()
        writer = _csv_mod.writer(buf)
        writer.writerow(["questions", "option1", "option2", "option3", "option4",
                          "answer", "explanation", "type", "section"])
        for row in all_mcqs_csv:
            writer.writerow(row)
        await send_document(chat_id, buf.getvalue().encode("utf-8"),
            f"{topic}_QBM.csv",
            caption=f"📋 {topic} — {len(all_mcqs_csv)} MCQ (Extracted)",
            mime_type="text/csv")

    if not csv_only and summary_pages:
        total_mcq_sum = sum(p["mcq_count"] for p in summary_pages)
        bd_time = _get_bd_time()
        summary = f"⚙️QBM Summary\n📋Topic: {topic}\n🚀Total Extracted MCQ: {total_mcq_sum}\n\n"
        for p in summary_pages:
            summary += f"🌟Page No: {fmt_page(p['page'])} ({p['mcq_count']} MCQ)\n{p['first_poll']}\n\n"
        summary += f"📅 {bd_time}"

        summary_data = {"chat_id": channel_id, "text": summary, "disable_web_page_preview": True}
        if first_image_msg_id:
            summary_data["reply_to_message_id"] = first_image_msg_id
        if thread_id:
            summary_data["message_thread_id"] = thread_id
        sum_r = await tg_post("sendMessage", summary_data)
        if sum_r.get("ok"):
            await try_pin_message(channel_id, sum_r["result"]["message_id"])

    elapsed = int(time.time() - start_time)
    mins, secs = divmod(elapsed, 60)
    page_breakdown_final = "\n".join(
        f"📌 Page {fmt_page(p)}: {ps['mcq']} MCQ" for p, ps in zip([pp for pp, _ in display_pages], page_status)
    )
    await edit_msg(chat_id, status_msg_id,
        f"✅ <b>QBM Extraction Complete!</b>\n\n📄 {file_name}\n📋 {topic}\n\n"
        f"{page_breakdown_final}\n\n"
        f"📝 Total MCQ Extracted: {total_mcq}\n📋 Pages: {len(display_pages)}\n⏱️ {mins}:{secs:02d}")

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
        await _safe_error_reply(chat_id, e)


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
            pdf_bytes = await _apply_saved_watermark(pdf_bytes)
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
        await _safe_error_reply(chat_id, e)


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
    exam_url = f"{GH_PAGES_EXAM_URL}?id={cache_id}"
    kb = {"inline_keyboard": [
        [{"text": "🔄 Same Quiz", "callback_data": f"qsame_{cache_id}"},
         {"text": "🆕 New Quiz", "callback_data": f"qnew_{cache_id}"}],
        [{"text": "🔄 Same Poll", "callback_data": f"pollagain_{cache_id}"},
         {"text": "🆕 New Poll", "callback_data": f"pollnew_{cache_id}"}],
        [{"text": "🌐 Website Exam", "url": exam_url}]
    ]}
    back_url = build_back_url(cache.get("channel_id", ""), source_msg_id(cache))
    if back_url:
        kb["inline_keyboard"].append([{"text": "↩️ Back to Source", "url": back_url}])
    return kb

async def handle_poll_again(cache_id: str, user: dict, chat_id: int):
    try:
        await _handle_poll_again_inner(cache_id, user, chat_id)
    except Exception as e:
        logger.error(f"[PollAgain] CRASHED cache={cache_id[:8]}: {e}")
        await notify_owner(f"⚠️ Poll Solve crashed (cache={cache_id[:8]}): {e}")

async def _handle_poll_again_inner(cache_id: str, user: dict, chat_id: int):
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
        f"🌟 Topic: {topic}\n📝 Total MCQ: {total}\n\n⏱️ Are you ready?"
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

    poll_fail_count = 0
    skipped_empty = 0
    for i, mcq in enumerate(mcqs):
        opts = mcq.get("options", [])
        q_raw = (mcq.get("question") or "").strip()
        if not q_raw or len(opts) < 2 or all(not (o or "").strip() for o in opts):
            skipped_empty += 1
            logger.warning(f"[PollAgain] skipped q{i+1}/{total}: empty question or options in cache")
            continue
        # pad missing/empty options up to 4, cap at 4 (A-D) — defensive, even if
        # cache has stale 5-option data from before the source-side cap existed
        opts = [(o or "").strip() or f"Option {j+1}" for j, o in enumerate(opts)][:4]
        ans_idx = {"A": 0, "B": 1, "C": 2, "D": 3}.get(mcq.get("answer", "A"), 0)
        ans_idx = min(ans_idx, len(opts) - 1)
        q_text = f"({i+1}/{total}) {q_raw}"
        if tag:
            q_text = f"{tag}\n\n{q_text}"
        exp = mcq.get("explanation", "")
        if exp_footer:
            exp = f"{exp}\n{exp_footer}"
        poll_res = await send_poll(chat_id, q_text, opts, ans_idx, explanation=exp)
        if not poll_res.get("ok"):
            poll_fail_count += 1
            logger.error(f"[PollAgain] sendPoll failed q{i+1}/{total}: {poll_res.get('description') or poll_res.get('error')}")
        await asyncio.sleep(1.5)
    if poll_fail_count > 0 or skipped_empty > 0:
        await notify_owner(
            f"⚠️ Poll Practice ({cache_id[:8]}): {poll_fail_count}/{total} poll পাঠাতে ব্যর্থ, "
            f"{skipped_empty} টা empty question/option থাকায় skip করা হয়েছে। Render logs চেক করুন।"
        )

    end_text = (
        f"✅ <b>Poll শেষ!</b>\n\n🎯 Topic: {topic}\n"
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

    AVG_GEN_SECONDS = 16
    started_at = time.time()
    img_bytes = await download_tg_file(image_file_id)
    from PIL import Image as PILImage
    img = PILImage.open(BytesIO(img_bytes))

    loading_caption = f"🆕 New Poll বানানো হচ্ছে...\n🌟 Topic: {topic}\n[░░░░░░░░░░ 0%]\nআনুমানিক {AVG_GEN_SECONDS}s বাকি..."
    loading_msg = await send_photo(chat_id, img_bytes, loading_caption)
    loading_id = loading_msg.get("result", {}).get("message_id")

    pct_box = {"pct": 8}

    async def update_progress():
        while pct_box["pct"] < 90:
            await asyncio.sleep(1)
            elapsed = time.time() - started_at
            pct_box["pct"] = min(90, pct_box["pct"] + 4)
            remaining = max(0, round(AVG_GEN_SECONDS - elapsed))
            bars = "█" * (pct_box["pct"] // 10) + "░" * (10 - pct_box["pct"] // 10)
            if loading_id:
                try:
                    await edit_msg_caption(chat_id, loading_id,
                        f"🆕 New Poll বানানো হচ্ছে...\n🌟 Topic: {topic}\n[{bars} {pct_box['pct']}%]\n{remaining}s বাকি...")
                except Exception:
                    pass

    progress_task = asyncio.create_task(update_progress())
    try:
        new_mcqs = _cap_mcq_options(await asyncio.wait_for(
            generate_new_mcq(img, topic, page, mcq_count=15), timeout=90))
    except Exception as e:
        progress_task.cancel()
        logger.error(f"[PollNew] generation failed: {e}")
        if loading_id:
            try:
                await edit_msg_caption(chat_id, loading_id, "❌ MCQ generate করতে সমস্যা হয়েছে, আবার চেষ্টা করো!")
            except Exception:
                pass
        else:
            await send_msg(chat_id, "❌ MCQ generate করতে সমস্যা হয়েছে, আবার চেষ্টা করো!")
        return
    progress_task.cancel()

    if not new_mcqs:
        if loading_id:
            try:
                await edit_msg_caption(chat_id, loading_id, "❌ MCQ generate হয়নি!")
            except Exception:
                pass
        else:
            await send_msg(chat_id, "❌ MCQ generate হয়নি!")
        return

    await db_increment_gen_count(cache_id, uid)
    if loading_id:
        try:
            await edit_msg_caption(chat_id, loading_id, f"✅ {len(new_mcqs)} টি নতুন MCQ ready!\n\nশুরু হচ্ছে...")
        except Exception:
            pass

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
        f"🌟 Topic: {topic}\n📝 Total MCQ: {total}\n\n⏱️ Are you ready?"
    )
    if image_file_id:
        r = await send_photo_by_id(chat_id, image_file_id, pre_caption, parse_mode="HTML")
        if not r.get("ok"):
            await send_msg(chat_id, pre_caption, parse_mode="HTML")
    else:
        await send_msg(chat_id, pre_caption)

    await send_msg(chat_id, "3️⃣ 2️⃣ 1️⃣ 🚀 শুরু!")
    await asyncio.sleep(1)

    poll_fail_count2 = 0
    for i, mcq in enumerate(new_mcqs):
        opts = mcq.get("options", [])[:4]
        ans_idx = {"A": 0, "B": 1, "C": 2, "D": 3}.get(mcq.get("answer", "A"), 0)
        q_text = f"({i+1}/{total}) {mcq['question']}"
        if tag:
            q_text = f"{tag}\n\n{q_text}"
        exp = mcq.get("explanation", "")
        if exp_footer:
            exp = f"{exp}\n{exp_footer}"
        poll_res2 = await send_poll(chat_id, q_text, opts, ans_idx, explanation=exp)
        if not poll_res2.get("ok"):
            poll_fail_count2 += 1
            logger.error(f"[PollNew] sendPoll failed q{i+1}/{total}: {poll_res2.get('description') or poll_res2.get('error')}")
        await asyncio.sleep(1.5)
    if poll_fail_count2 > 0:
        await notify_owner(f"⚠️ New Poll ({new_cache_id[:8]}): {poll_fail_count2}/{total} poll পাঠাতে ব্যর্থ হয়েছে। Render logs চেক করুন।")

    remaining_new = 5 - (count + 1)
    kb = _poll_end_kb(new_cache_id, new_cache or cache)
    kb["inline_keyboard"][1][0]["text"] = f"🆕 New Poll ({remaining_new} বাকি)"

    end_text = (
        f"✅ <b>New Poll শেষ!</b>\n\n🎯 Topic: {topic}\n"
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
_QUIZ_START_LOCK: set = set()  # v4.1: per-uid debounce — prevents double-tap/duplicate
                                # callback_query delivery from starting two overlapping
                                # quiz sessions (qmis/qspe/qnew), which looked like
                                # "more MCQ than it should have" since both sessions'
                                # poll loops ran at once.

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

async def _run_quiz_start_debounced(coro, uid: int):
    """v4.1: runs a quiz-start coroutine then releases the debounce lock,
    even on error, so a single failed start doesn't permanently block uid."""
    try:
        await coro
    except Exception as e:
        logger.error(f"[QuizStart] debounced run error: {e}")
    finally:
        _QUIZ_START_LOCK.discard(uid)

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
        "image_file_id": cache.get("image_file_id"),
        "start": time.time(), "poll_id": None, "answered": False, "timer_task": None
    }
    await qs_set(uid, state)

    topic = cache["topic"]
    page = cache["page_number"]
    total = len(mcqs)

    pre_caption = (
        f"{title}\n\n🌟 Topic: {topic}\n"
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
    try:
        await _send_quiz_question_inner(uid)
    except Exception as e:
        logger.error(f"[QuizSolve] _send_quiz_question CRASHED for uid={uid}: {e}")
        st = await qs_get(uid)
        if st:
            await _finish_quiz(uid)
        await notify_owner(f"⚠️ Quiz Solve crashed: {e}")

async def _send_quiz_question_inner(uid: int):
    st = await qs_get(uid)
    if not st:
        return
    st["timer_task"] = None

    i = st["idx"]
    mcq = st["mcqs"][i]
    opts = mcq.get("options", [])
    q_raw = (mcq.get("question") or "").strip()

    # Malformed MCQ (empty question / insufficient options) — skip it, don't crash the quiz
    if not q_raw or len(opts) < 2 or all(not (o or "").strip() for o in opts):
        logger.warning(f"[QuizSolve] skipped malformed q{i+1}/{len(st['mcqs'])}: empty question/options")
        st["idx"] += 1
        st["skip"] += 1
        st["skip_idx"].append(i)
        await qs_set(uid, st)
        if st["idx"] >= len(st["mcqs"]):
            await _finish_quiz(uid)
        else:
            await _send_quiz_question(uid)
        return

    opts = [(o or "").strip() or f"Option {j+1}" for j, o in enumerate(opts)][:4]
    ans_idx = {"A": 0, "B": 1, "C": 2, "D": 3}.get(mcq.get("answer", "A"), 0)
    ans_idx = min(ans_idx, len(opts) - 1)
    total = len(st["mcqs"])

    q_text = f"({i+1}/{total}) {q_raw}"
    if st["tag"]:
        q_text = f"{st['tag']}\n\n{q_text}"

    exp = mcq.get("explanation", "")
    if st["exp_footer"]:
        exp = f"{exp}\n{st['exp_footer']}"

    poll_r = await send_poll(
        st["chat_id"], q_text[:300], [o[:100] for o in opts], ans_idx,
        explanation=exp[:200], is_anonymous=False, open_period=QUIZ_Q_SEC
    )

    if not poll_r.get("ok"):
        logger.error(f"[QuizSolve] sendPoll failed q{i+1}/{total}: {poll_r.get('description') or poll_r.get('error')}")
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
    asyncio.create_task(db_save_last_quiz(uid, st))

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
        asyncio.create_task(db_save_leaderboard(cache_id, uid, st["uname"], st["topic"], st["page"], right, total, fin))

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

    exam_url = f"{GH_PAGES_EXAM_URL}?id={cache_id}&uid={uid}&name={st['uname']}"
    back_url = build_back_url(st["channel_id"], st["back_msg_id"])
    wrong_count = len(st["wrong_idx"])
    skip_count = len(st["skip_idx"])
    special_count = len(set(st["wrong_idx"] + st["skip_idx"]))

    kb = {"inline_keyboard": []}
    has_image = bool(st.get("image_file_id"))
    # Item 4: fixed 3-row layout
    kb["inline_keyboard"].append([
        {"text": "🔄 Same Quiz", "callback_data": f"qsame_{cache_id}"},
    ] + ([{"text": "🆕 New Quiz", "callback_data": f"qnew_{cache_id}"}] if has_image else []))
    kb["inline_keyboard"].append([
        {"text": "🔄 Same Poll", "callback_data": f"pollagain_{cache_id}"},
    ] + ([{"text": "🆕 New Poll", "callback_data": f"pollnew_{cache_id}"}] if has_image else []))
    kb["inline_keyboard"].append([{"text": "🌐 Website Exam", "url": exam_url}])
    if wrong_count > 0:
        kb["inline_keyboard"].append([{"text": f"❌ Mistake Practice ({wrong_count} টি ভুল)", "callback_data": "qmis"}])
    if special_count > 0:
        kb["inline_keyboard"].append([{"text": f"🔥 Special Practice ({special_count} টি wrong+skip)", "callback_data": "qspe"}])
    if not st["is_new_gen"] and st["mode"] == "quiz":
        kb["inline_keyboard"].append([{"text": "🏆 Leaderboard দেখো", "callback_data": f"polllb_{cache_id}"}])
    if back_url:
        kb["inline_keyboard"].append([{"text": "↩️ Back to Source", "url": back_url}])

    img_id = st.get("image_file_id")

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

async def handle_quiz_same(cache_id: str, user: dict, chat_id: int):
    """Item 4: 'Same Quiz' button — replay the exact same cached MCQ set as a quiz."""
    uid = user["id"]
    uname = user.get("username") or user.get("first_name", "User")
    await db_track_user(uid, uname)
    await start_sequential_quiz(chat_id, uid, uname, cache_id, title="🔄 <b>Same Quiz আবার শুরু হচ্ছে!</b>")

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
    AVG_GEN_SECONDS = 16
    started_at = time.time()
    img_bytes = await download_tg_file(image_file_id)
    from PIL import Image as PILImage
    img = PILImage.open(BytesIO(img_bytes))

    loading_caption = f"🆕 New Quiz বানানো হচ্ছে...\n🌟 Topic: {cache['topic']}\n[░░░░░░░░░░ 0%]\nআনুমানিক {AVG_GEN_SECONDS}s বাকি..."
    loading = await send_photo(chat_id, img_bytes, loading_caption)
    loading_id = loading.get("result", {}).get("message_id")

    pct_box = {"pct": 8}

    async def update_progress():
        while pct_box["pct"] < 90:
            await asyncio.sleep(1)
            elapsed = time.time() - started_at
            pct_box["pct"] = min(90, pct_box["pct"] + 4)
            remaining = max(0, round(AVG_GEN_SECONDS - elapsed))
            bars = "█" * (pct_box["pct"] // 10) + "░" * (10 - pct_box["pct"] // 10)
            if loading_id:
                try:
                    await edit_msg_caption(chat_id, loading_id,
                        f"🆕 New Quiz বানানো হচ্ছে...\n🌟 Topic: {cache['topic']}\n[{bars} {pct_box['pct']}%]\n{remaining}s বাকি...")
                except Exception:
                    pass

    progress_task = asyncio.create_task(update_progress())
    try:
        new_mcqs = _cap_mcq_options(await asyncio.wait_for(
            generate_new_mcq(img, cache["topic"], cache["page_number"], mcq_count=15), timeout=90))
    except Exception as e:
        progress_task.cancel()
        logger.error(f"[QuizNew] generation failed: {e}")
        if loading_id:
            try:
                await edit_msg_caption(chat_id, loading_id, "❌ MCQ generate করতে সমস্যা হয়েছে, আবার চেষ্টা করো!")
            except Exception:
                pass
        else:
            await send_msg(chat_id, "❌ MCQ generate করতে সমস্যা হয়েছে, আবার চেষ্টা করো!")
        return
    progress_task.cancel()

    if not new_mcqs:
        if loading_id:
            try:
                await edit_msg_caption(chat_id, loading_id, "❌ MCQ generate হয়নি!")
            except Exception:
                pass
        else:
            await send_msg(chat_id, "❌ MCQ generate হয়নি!")
        return
    await db_increment_gen_count(cache_id, uid)
    new_cache_id = gen_session_id()
    await db_save_mcq_cache(new_cache_id, new_cache_id, cache["page_number"], cache["topic"],
                            new_mcqs, [], image_file_id, cache.get("image_msg_id"),
                            cache.get("channel_id"), is_new_gen=True, end_msg_id=cache.get("end_msg_id"))
    if loading_id:
        try:
            await edit_msg_caption(chat_id, loading_id, f"✅ {len(new_mcqs)} টি নতুন MCQ ready!")
        except Exception:
            pass
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
            await _safe_error_reply(chat_id, e)
        return

    await send_msg(chat_id,
        "🔗 CSV ফাইলে reply করে /merge দাও\n"
        "/merge done — merge করো\n"
        "/merge status — count দেখো\n"
        "/merge cancel — বাতিল"
    )


async def handle_error_command(msg: dict):
    """Owner/Admin only — আজকের error log file-এর শেষ অংশ raw text হিসেবে
    দেখায় (AtlasBot-style, simple file-based, কোনো DB/structured parsing ছাড়াই)."""
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

    content = await get_recent_errors()
    if not content.strip():
        await send_msg(chat_id, "✅ আজ কোনো error নেই!")
        return

    tail = content[-3800:]
    await send_msg(chat_id, f"🚨 Latest Errors:\n\n{tail}")


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
                    ans, _strip_img_tag(item.get("explanation", "")), "1", "1"
                ])
            await send_document(chat_id, buf.getvalue().encode("utf-8"),
                file_name.replace(".json", ".csv"),
                caption=f"✅ JSON → CSV Converted! {len(json_data)} questions",
                mime_type="text/csv")
        else:
            await send_msg(chat_id, "❌ Only CSV or JSON files!")
    except Exception as e:
        await _safe_error_reply(chat_id, e)


# ============================================================
# WEBHOOK HANDLER
# ============================================================
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

@app.post("/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return Response(status_code=403)
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

    # Auto mhtml/html → smart detect (MCQ→CSV queue, Q&A/CQ→tell user to use /qpdf)
    if msg.get("document") and not msg.get("reply_to_message"):
        _dfn = msg["document"].get("file_name", "").lower()
        if _dfn.endswith(".mhtml") or _dfn.endswith(".mht") or _dfn.endswith(".html") or _dfn.endswith(".htm"):
            await _mhtml_auto_queue.put(msg)
            qsize = _mhtml_auto_queue.qsize()
            if qsize > 1:
                await send_msg(chat_id, f"📥 Queue-তে যোগ হয়েছে (position: {qsize})")
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

    # Channel rename flow check (awaiting new name text after ✏️ Name Update tap)
    if uid in CHANNEL_RENAME_PENDING and msg.get("text") and not text.startswith("/"):
        channel_id = CHANNEL_RENAME_PENDING.pop(uid)
        new_name = text.strip()
        ok = await db_rename_channel(channel_id, new_name)
        if ok:
            await send_msg(chat_id, f"✅ <code>{channel_id}</code> এর নাম আপডেট হয়েছে: <b>{new_name}</b>")
            await _show_channel_list(chat_id)
        else:
            await send_msg(chat_id, "❌ Rename failed!")
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
    if text.startswith("/start pdfnew_"):
        cache_id = text.replace("/start pdfnew_", "").strip()
        if uid not in _QUIZ_START_LOCK:
            _QUIZ_START_LOCK.add(uid)
            asyncio.create_task(_run_quiz_start_debounced(handle_quiz_new(cache_id, msg["from"], chat_id), uid))
        return
    if text.startswith("/start pollnew_"):
        cache_id = text.replace("/start pollnew_", "").strip()
        if uid not in _QUIZ_START_LOCK:
            _QUIZ_START_LOCK.add(uid)
            asyncio.create_task(_run_quiz_start_debounced(handle_poll_new(cache_id, msg["from"], chat_id, None), uid))
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
        arg = text.replace("/pdf", "").strip().lower()
        if arg in ("on", "off"):
            await handle_pdf_autosend_toggle(msg, arg)
            return
        clear_cancel(chat_id)
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
    elif text.lower().startswith("/tagq"):
        await handle_tagQ(msg)
    elif text.lower().startswith("/expq"):
        await handle_expQ(msg)
    elif text.startswith("/channel") or text == "/channelist":
        await handle_channel(msg)
    elif text == "/info2":
        await handle_info2(msg)
    elif text.startswith("/qbmprompt"):
        # /qbmprompt <new prompt text> -> permanently overrides the active
        # QBM extraction prompt (Call 1). Persists in Supabase (quiz_sessions,
        # key="qbm_active_prompt") so it's remembered on every future page/
        # future run, until the next /qbmprompt update.
        # /qbmprompt reset -> restores the built-in default prompt.
        # /qbmprompt (no args) -> shows the currently active prompt.
        new_prompt = text[len("/qbmprompt"):].strip()
        if not new_prompt:
            await send_msg(chat_id, f"📋 Active QBM Prompt:\n\n<code>{qbm_get_active_prompt()[:3500]}</code>")
        elif new_prompt.lower() == "reset":
            qbm_set_active_prompt(QBM_EXTRACT_PROMPT_DEFAULT)
            await send_msg(chat_id, "✅ QBM prompt default-এ reset হয়ে গেছে।")
        else:
            qbm_set_active_prompt(new_prompt)
            await send_msg(chat_id, "✅ QBM prompt permanently update হয়ে গেছে। এখন থেকে সব page-এ এই prompt-ই ব্যবহার হবে।")
    elif text.startswith("/qbm"):
        # /qbm = Question Bank Maker — EXTRACTS existing MCQ from PDF (never generates new)
        # 100% ported from AtlasMasterBot's qbm_handler
        asyncio.create_task(handle_qbm(msg))
    elif text.startswith("/pdfm"):
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        clear_cancel(chat_id)
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
    elif text.startswith("/cancel"):
        asyncio.create_task(handle_cancel_command(msg))
    elif text.startswith("/sheet"):
        asyncio.create_task(handle_sheet_command(msg))
    elif text.startswith("/qcsv"):
        asyncio.create_task(handle_qcsv_command(msg))
    elif text.startswith("/qpdf"):
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        asyncio.create_task(handle_qpdf_command(msg))
    elif text.startswith("/split"):
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        asyncio.create_task(handle_split_command(msg))
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
    elif text.startswith("/live") and not text.startswith("/livetime"):
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
    elif text.startswith("/watermark"):
        if not is_auth:
            await send_msg(chat_id, UNAUTH_MSG)
            return
        wm_arg = re.sub(r"^/watermark\s*", "", text, flags=re.IGNORECASE).strip()
        reply = msg.get("reply_to_message")
        if wm_arg and reply and reply.get("document"):
            # New: /watermark reply-to-PDF + name -> apply immediately, no step-by-step wait
            asyncio.create_task(handle_wm_command(msg))
        else:
            await handle_watermark_command(msg)
    elif text == "/convert":
        await handle_convert_command(msg)
    elif text.startswith("/error") or text.startswith("/errors"):
        await handle_error_command(msg)
    elif text == "/ping":
        try:
            _t0 = time.time()
            _r = await send_msg(chat_id, "🏓 Pong!")
            _msg_id = _r.get("result", {}).get("message_id") if isinstance(_r, dict) else None

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

            import os as _os, httpx as _hx
            _platform = _os.environ.get("RUNNING_ON", "") or "HuggingFace Space"

            # Current webhook check
            _wh_url = "Unknown"
            try:
                async with _hx.AsyncClient(timeout=5) as _c:
                    _wr = await _c.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo")
                    _wh_data = _wr.json()
                    _wh_url = _wh_data.get("result", {}).get("url", "Not set") or "Not set"
                    _render_primary = (os.environ.get("RENDER_URL", "") or "").replace("https://", "").replace("http://", "").rstrip("/")
                    _render_secondary = (os.environ.get("RENDER_URL_2", "") or "").replace("https://", "").replace("http://", "").rstrip("/")
                    if _render_secondary and _render_secondary in _wh_url:
                        _wh_short = "🟠 Render SECONDARY (failover active!)"
                    elif _render_primary and _render_primary in _wh_url:
                        _wh_short = "🟡 Render PRIMARY"
                    elif "onrender.com" in _wh_url:
                        _wh_short = "🟡 Render (unknown account)"
                    elif "workers.dev" in _wh_url or "pages.dev" in _wh_url:
                        _wh_short = "🟢 CF Worker (normal)"
                    elif "hf.space" in _wh_url:
                        _wh_short = "🔵 HF Space (direct)"
                    else:
                        _wh_short = f"⚪ {_wh_url[:40]}"
            except Exception:
                _wh_short = "❓ Check failed"

            _latency_ms = int((time.time() - _t0) * 1000)
            final_text = (
                "🏓 <b>Pong! ATLAS QuizBot Online</b>\n\n"
                f"⚡ <b>Latency:</b> {_latency_ms}ms\n"
                f"🖥 <b>Running on:</b> {_platform}\n"
                f"🔗 <b>Webhook:</b> {_wh_short}\n"
                f"🕐 চালু হয়েছে: {started_at}\n"
                f"⏱ Active আছে: {uptime_str}\n"
                f"🔑 Gemini Keys: {key_count}\n"
                f"👥 Total Users: {total_users}\n"
                f"🟢 আজকে Active: {daily_active}"
            )
            if _msg_id:
                await edit_msg(chat_id, _msg_id, final_text, parse_mode="HTML")
            else:
                await send_msg(chat_id, final_text, parse_mode="HTML")
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
        if data.startswith("chsel_"):
            channel_id = data[len("chsel_"):]
            await _show_channel_actions(chat_id, msg_id, channel_id)
            return
        if data == "chback":
            await _show_channel_list(chat_id, edit_message_id=msg_id)
            return
        if data.startswith("chdel_"):
            channel_id = data[len("chdel_"):]
            ok = await db_delete_channel(channel_id)
            if ok:
                await tg_post("editMessageText", {"chat_id": chat_id, "message_id": msg_id,
                    "text": f"✅ Channel <code>{channel_id}</code> delete করা হয়েছে।", "parse_mode": "HTML"})
                await _show_channel_list(chat_id)
            else:
                await tg_post("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "❌ Delete failed!"})
            return
        if data.startswith("chren_"):
            channel_id = data[len("chren_"):]
            CHANNEL_RENAME_PENDING[uid] = channel_id
            await tg_post("editMessageText", {"chat_id": chat_id, "message_id": msg_id,
                "text": f"✏️ <code>{channel_id}</code> এর নতুন নাম লিখে পাঠাও:", "parse_mode": "HTML"})
            return
        if data.startswith("sheetstyle:"):
            await handle_sheet_style_callback(query)
            return
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

            if channel == "csv":
                saved_thread_id = pending.get("thread_id")
                cached_pages = getattr(app.state, "pdf_cache", {}).get(f"pdf_img_{uid}")
                if cached_pages:
                    await process_pdf_pages(chat_id, uid, user.get("first_name", "User"), cached_pages,
                        pending["topic"], pending.get("mcq_count"), None, True,
                        pending.get("file_name", "document.pdf"), pending.get("status_msg_id"),
                        thread_id=saved_thread_id, skip_generate=True)
                    getattr(app.state, "pdf_cache", {}).pop(f"pdf_img_{uid}", None)
                    return
                saved_file_id = pending.get("file_id")
                if not saved_file_id:
                    await send_msg(chat_id, "❌ Session expired!")
                    return
                await send_msg(chat_id, "⏳ PDF re-download হচ্ছে...")
                try:
                    pdf_bytes = await _download_pdf_cached(saved_file_id)
                    ok, pages = await asyncio.to_thread(_render_pdf_cached, saved_file_id, pdf_bytes, pending.get("page_range"))
                except Exception as e:
                    await send_msg(chat_id, f"❌ PDF re-download failed: {e}")
                    return
                if not ok:
                    await send_msg(chat_id, pages)
                    return
                if not pages:
                    await send_msg(chat_id, "❌ Page পাওয়া যায়নি!")
                    return
                await process_pdf_pages(chat_id, uid, user.get("first_name", "User"), pages,
                    pending["topic"], pending.get("mcq_count"), None, True,
                    pending.get("file_name", "document.pdf"), pending.get("status_msg_id"),
                    thread_id=saved_thread_id)
                return

            # ── NEW STEP: channel picked -> ask With Image vs Without Image
            # (Without Image = photo skipped, only MCQ polls go to channel,
            # same pattern as /img's Topic Mode) ──
            pending["channel_id"] = channel
            sb.table("quiz_sessions").upsert({
                "key": f"pdf_pending_{uid}",
                "data": json.dumps(pending),
                "updated_at": int(time.time())
            }).execute()
            kb = {"inline_keyboard": [
                [{"text": "🖼️ With Image (present system)", "callback_data": f"pdfimg_with_{uid}"}],
                [{"text": "📝 Without Image (শুধু MCQ Poll)", "callback_data": f"pdfimg_without_{uid}"}]
            ]}
            await send_msg(chat_id, "কোন mode-এ পাঠাবে?", reply_markup=kb)

        elif data.startswith("pdfimg_"):
            parts = data.split("_")
            img_choice = parts[1]  # "with" or "without"
            orig_uid = int(parts[2])
            if uid != orig_uid:
                return
            row = sb.table("quiz_sessions").select("data").eq("key", f"pdf_pending_{uid}").execute()
            if not row.data:
                await send_msg(chat_id, "❌ Session expired!")
                return
            pending = json.loads(row.data[0]["data"])
            channel = pending.get("channel_id")
            saved_thread_id = pending.get("thread_id")
            cached_pages = getattr(app.state, "pdf_cache", {}).get(f"pdf_img_{uid}")
            if cached_pages:
                await process_pdf_pages(chat_id, uid, user.get("first_name", "User"), cached_pages,
                    pending["topic"], pending.get("mcq_count"), channel, False,
                    pending.get("file_name", "document.pdf"), pending.get("status_msg_id"),
                    thread_id=saved_thread_id, with_image=(img_choice == "with"), skip_generate=True)
                getattr(app.state, "pdf_cache", {}).pop(f"pdf_img_{uid}", None)
                return
            saved_file_id = pending.get("file_id")
            if not saved_file_id:
                await send_msg(chat_id, "❌ Session expired!")
                return
            await send_msg(chat_id, "⏳ PDF re-download হচ্ছে...")
            try:
                pdf_bytes = await _download_pdf_cached(saved_file_id)
                ok, pages = await asyncio.to_thread(_render_pdf_cached, saved_file_id, pdf_bytes, pending.get("page_range"))
                if not ok:
                    await send_msg(chat_id, pages)
                    return
            except Exception as e:
                await send_msg(chat_id, f"❌ PDF re-download failed: {e}")
                return
            if not pages:
                await send_msg(chat_id, "❌ Page পাওয়া যায়নি!")
                return
            await process_pdf_pages(chat_id, uid, user.get("first_name", "User"), pages,
                pending["topic"], pending.get("mcq_count"), channel, False,
                pending.get("file_name", "document.pdf"), pending.get("status_msg_id"),
                thread_id=saved_thread_id, with_image=(img_choice == "with"))

        elif data.startswith("pollagain_"):
            cache_id = data.replace("pollagain_", "")
            asyncio.create_task(handle_poll_again(cache_id, user, chat_id))

        elif data.startswith("qsame_"):
            cache_id = data.replace("qsame_", "")
            asyncio.create_task(handle_quiz_same(cache_id, user, chat_id))

        elif data.startswith("pollnew_"):
            cache_id = data.replace("pollnew_", "")
            if uid in _QUIZ_START_LOCK:
                return
            _QUIZ_START_LOCK.add(uid)
            asyncio.create_task(_run_quiz_start_debounced(handle_poll_new(cache_id, user, chat_id, msg_id), uid))

        elif data.startswith("polllb_"):
            cache_id = data.replace("polllb_", "")
            await handle_poll_leaderboard(cache_id, uid, chat_id)

        elif data.startswith("qnew_"):
            cache_id = data.replace("qnew_", "")
            if uid in _QUIZ_START_LOCK:
                return
            _QUIZ_START_LOCK.add(uid)
            asyncio.create_task(_run_quiz_start_debounced(handle_quiz_new(cache_id, user, chat_id), uid))

        elif data == "qmis":
            if uid in _QUIZ_START_LOCK:
                return
            _QUIZ_START_LOCK.add(uid)
            asyncio.create_task(_run_quiz_start_debounced(handle_quiz_practice(uid, chat_id, uname, "mis"), uid))

        elif data == "qspe":
            if uid in _QUIZ_START_LOCK:
                return
            _QUIZ_START_LOCK.add(uid)
            asyncio.create_task(_run_quiz_start_debounced(handle_quiz_practice(uid, chat_id, uname, "spe"), uid))

        elif data == "bm_pdf":
            fake_msg = {"chat": {"id": chat_id}, "from": {"id": uid, "first_name": uname}}
            await handle_bm(fake_msg)

        elif data == "bmexam_again":
            fake_msg = {"chat": {"id": chat_id}, "from": {"id": uid, "first_name": uname}}
            asyncio.create_task(handle_bmexam(fake_msg))

        elif data.startswith("imgsrc_"):
            parts = data.split("_")
            source = parts[1]  # "new" or "existing"
            orig_uid = int(parts[2])
            if uid != orig_uid:
                return
            await handle_img_source(source, uid, chat_id, user)

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
            img_data["channel"] = channel
            sb.table("quiz_sessions").upsert({
                "key": f"img_mode_{uid}",
                "data": json.dumps(img_data),
                "updated_at": int(time.time())
            }).execute()
            kb = {"inline_keyboard": [
                [{"text": "🖼️ Image Mode (image সহ channel-এ যাবে)", "callback_data": f"imgfinal_image_{uid}"}],
                [{"text": "📝 Topic Mode (শুধু MCQ Poll)", "callback_data": f"imgfinal_topic_{uid}"}]
            ]}
            await send_msg(chat_id,
                f"📌 Topic: <b>{img_data.get('topic', 'ATLAS Special MCQ')}</b>\n\nকোন mode-এ পাঠাবে?",
                reply_markup=kb, parse_mode="HTML"
            )

        elif data.startswith("imgfinal_"):
            parts = data.split("_")
            mode = parts[1]  # "image" or "topic"
            orig_uid = int(parts[2])
            if uid != orig_uid:
                return
            row = sb.table("quiz_sessions").select("data").eq("key", f"img_mode_{uid}").execute()
            if not row.data:
                await send_msg(chat_id, "❌ Session expired!")
                return
            img_data = json.loads(row.data[0]["data"])
            channel = img_data.get("channel")
            if not channel:
                await send_msg(chat_id, "❌ Channel select করা হয়নি!")
                return
            asyncio.create_task(process_img_to_poll(
                img_data["file_id"], channel, mode,
                chat_id, uid, uname, topic=img_data.get("topic", "ATLAS Special MCQ"),
                source=img_data.get("source", "new"), mcq_count=img_data.get("mcq_count")
            ))

        elif data.startswith("txtchannel_"):
            parts = data.split("_", 2)
            channel = parts[1]
            orig_uid = int(parts[2])
            if uid != orig_uid:
                return
            asyncio.create_task(process_txt_to_poll(channel, chat_id, uid, uname))

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
                    web_url = f"https://hamza818483-dotcom.github.io/QuizBot/exam.html?id={quiz_id}"
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
                    pdf_bytes = await _download_pdf_cached(saved_file_id)
                    ok, pages = await asyncio.to_thread(_render_pdf_cached, saved_file_id, pdf_bytes, pending.get("page_range"))
                    if not ok:
                        await send_msg(chat_id, pages)
                        return
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

        elif data.startswith("qbmch_"):
            parts = data.split("_")
            channel = parts[1]
            orig_uid = int(parts[2])
            if uid != orig_uid:
                return
            row = sb.table("quiz_sessions").select("data").eq("key", f"qbm_pending_{uid}").execute()
            if not row.data:
                await send_msg(chat_id, "❌ Session expired!")
                return
            pending = json.loads(row.data[0]["data"])
            pages = getattr(app.state,"qbm_cache",{}).get(f"qbm_img_{uid}")
            csv_only = channel == "csv"
            ch = None if csv_only else channel
            if pages:
                # Cache hit: `pages` is already the extracted (page_num, img, mcqs)
                # tuples from Phase 1 — skip re-extraction entirely.
                asyncio.create_task(process_qbm_pages(
                    chat_id, uid, user.get("first_name","User"), pages,
                    pending["topic"], ch, csv_only,
                    pending.get("file_name","document.pdf"),
                    pending.get("status_msg_id"),
                    thread_id=pending.get("thread_id"),
                    skip_extract=True
                ))
            else:
                # Cache expired -> re-download and re-run the full 3-call
                # extraction pipeline (Phase 1) before posting.
                saved_file_id = pending.get("file_id")
                if not saved_file_id:
                    await send_msg(chat_id, "❌ Session expired!")
                    return
                await send_msg(chat_id, "⏳ PDF re-download হচ্ছে...")
                try:
                    pdf_bytes = await _download_pdf_cached(saved_file_id)
                    raw_ok, raw_pages = await asyncio.to_thread(_render_pdf_cached, saved_file_id, pdf_bytes, pending.get("page_range"))
                    if not raw_ok:
                        await send_msg(chat_id, raw_pages)
                        return
                except Exception as e:
                    await send_msg(chat_id, f"❌ PDF re-download failed: {e}")
                    return
                if not raw_pages:
                    await send_msg(chat_id, "❌ Page পাওয়া যায়নি!")
                    return
                async def _reextract_and_post():
                    extracted = await qbm_extract_all_pages(
                        chat_id, raw_pages, pending["topic"],
                        pending.get("file_name","document.pdf"),
                        pending.get("status_msg_id")
                    )
                    await process_qbm_pages(
                        chat_id, uid, user.get("first_name","User"), extracted,
                        pending["topic"], ch, csv_only,
                        pending.get("file_name","document.pdf"),
                        pending.get("status_msg_id"),
                        thread_id=pending.get("thread_id"),
                        skip_extract=True
                    )
                asyncio.create_task(_reextract_and_post())
            getattr(app.state,"qbm_cache",{}).pop(f"qbm_img_{uid}", None)

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
        await _safe_error_reply(chat_id, e)

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
        # v4.2: Web Quiz link-e click korlei instant exam shuru hobe —
        # source (Telegram uid soho/chara) jai hok na keno, pre-exam
        # screen ekdomi skip kora hoy.
        force_autostart = True
        with open("/app/index.html", "r", encoding="utf-8") as f:
            html = f.read()
        html = html.replace("{{CACHE_ID}}", cache_id)
        html = html.replace("{{USER_ID}}", uid)
        html = html.replace("{{USER_NAME}}", name)
        html = html.replace("{{SUPABASE_URL}}", SUPABASE_URL)
        html = html.replace("{{SUPABASE_KEY}}", SUPABASE_KEY)
        html = html.replace("{{HF_SPACE_URL}}", CF_WORKER_URL)
        if force_autostart:
            html = html.replace("<script>", "<script>window.__FORCE_AUTOSTART__=true;", 1)
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
                # Layer 2: Supabase Primary backup থেকে restore
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
                    logger.warning(f"[exam] Supabase Primary restore failed: {_e}")

            if not rows:
                # Layer 2b: Supabase Secondary backup থেকে restore
                try:
                    import httpx as _hx
                    _SB2_URL = "https://xnkuuzstschdovcyomfk.supabase.co"
                    _SB2_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inhua3V1enN0c2NoZG92Y3lvbWZrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI3NTI3NzUsImV4cCI6MjA5ODMyODc3NX0.rD6p4U1fdqnM2M6t7wA3qsMY1p3KEFD2S1WzSIZehW4"
                    _h2 = {"apikey": _SB2_KEY, "Authorization": f"Bearer {_SB2_KEY}"}
                    async with _hx.AsyncClient(timeout=10) as _c:
                        _r2 = await _c.get(f"{_SB2_URL}/rest/v1/quiz_backups",
                            headers=_h2, params={"quiz_id": f"eq.{cache_id}", "select": "*"})
                    _b2 = _r2.json()
                    if _b2:
                        _bk2 = _b2[0]
                        await d1_run(
                            "INSERT OR REPLACE INTO quizzes (id,name,description,timer,shuffle,csv_data,tag,exp_footer,created_by) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                            [cache_id, _bk2["name"], "", 30, 0, json.dumps(_bk2["questions"]), "", "", 0]
                        )
                        rows = await d1_select("SELECT * FROM quizzes WHERE id=?1", [cache_id])
                except Exception as _e:
                    logger.warning(f"[exam] Supabase Secondary restore failed: {_e}")

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
        pdf_bytes = await _apply_saved_watermark(pdf_bytes)
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
        try:
            from core import _ensure_d1_table, d1_run as _d1r
            await _ensure_d1_table("web_exam_results",
                "CREATE TABLE IF NOT EXISTS web_exam_results (id INTEGER PRIMARY KEY AUTOINCREMENT, cache_id TEXT, "
                "user_id INTEGER, user_name TEXT, topic TEXT, page_number INTEGER, total INTEGER, correct INTEGER, "
                "wrong INTEGER, skipped INTEGER, negative_marks REAL, final_score REAL, time_taken INTEGER)")
            await _d1r(
                "INSERT INTO web_exam_results (cache_id,user_id,user_name,topic,page_number,total,correct,wrong,skipped,negative_marks,final_score,time_taken) "
                "VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)",
                [cache_id, user_id, user_name, topic, page, total, correct, wrong, skipped, negative, final_score, time_taken]
            )
        except Exception as e:
            logger.warning(f"[D1] save_exam_result mirror warn: {e}")
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
        try:
            from core import _ensure_d1_table, d1_run as _d1r
            import json as _json
            await _ensure_d1_table("bookmarks",
                "CREATE TABLE IF NOT EXISTS bookmarks (user_id INTEGER, cache_id TEXT, question_index INTEGER, "
                "question_data TEXT, topic TEXT, page_number INTEGER, PRIMARY KEY (user_id, cache_id, question_index))")
            await _d1r(
                "INSERT INTO bookmarks (user_id,cache_id,question_index,question_data,topic,page_number) "
                "VALUES (?1,?2,?3,?4,?5,?6) "
                "ON CONFLICT(user_id,cache_id,question_index) DO UPDATE SET question_data=excluded.question_data, "
                "topic=excluded.topic, page_number=excluded.page_number",
                [data["user_id"], data.get("cache_id"), data.get("question_index"),
                 _json.dumps(data.get("question_data")), data.get("topic"), data.get("page")]
            )
        except Exception as e:
            logger.warning(f"[D1] save_bookmark mirror warn: {e}")
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
        try:
            from core import d1_run as _d1r
            await _d1r(
                "DELETE FROM bookmarks WHERE user_id=?1 AND cache_id=?2 AND question_index=?3",
                [data["user_id"], data["cache_id"], data["question_index"]]
            )
        except Exception as e:
            logger.warning(f"[D1] delete_bookmark mirror warn: {e}")
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/mhtml-status/{job_id}")
async def get_mhtml_status(job_id: str):
    job = MHTML_JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "job_not_found"}, status_code=404)
    return JSONResponse({
        "ok": True,
        "status": job["status"],
        "phase": job.get("phase", "parsing"),
        "done": job["done"],
        "total": job["total"],
        "pct": job["pct"],
        "eta_sec": job["eta_sec"],
        "source": job["source"],
        "file_name": job["file_name"],
        "error": job.get("error"),
        "dl_done": job.get("dl_done", 0),
        "dl_total": job.get("dl_total", 0),
        "dl_speed": job.get("dl_speed", 0),
    })


@app.get("/mhtml-status/{job_id}", response_class=HTMLResponse)
async def mhtml_status_page(job_id: str):
    html = """<!DOCTYPE html>
<html lang="bn"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCQ Processing Live Dashboard</title>
<style>
body{font-family:sans-serif;background:#0f172a;color:#e2e8f0;display:flex;
  align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:#1e293b;padding:32px;border-radius:16px;max-width:420px;width:90%;
  box-shadow:0 8px 24px rgba(0,0,0,.4)}
h2{margin:0 0 20px;font-size:20px;text-align:center}
.bar-wrap{background:#334155;border-radius:8px;height:22px;overflow:hidden;margin-bottom:16px}
.bar{height:100%;background:linear-gradient(90deg,#22c55e,#16a34a);width:0%;
  transition:width .4s;display:flex;align-items:center;justify-content:center;
  font-size:12px;font-weight:600;color:#fff}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:14px}
.stat{background:#0f172a;padding:10px;border-radius:8px;text-align:center}
.stat b{display:block;font-size:18px;color:#22c55e}
#status-msg{text-align:center;margin-top:14px;font-size:13px;color:#94a3b8}
</style></head>
<body>
<div class="card">
<h2>📊 MCQ Live Processing</h2>
<div class="bar-wrap"><div class="bar" id="bar">0%</div></div>
<div class="stats">
  <div class="stat"><b id="done">0</b>হয়েছে</div>
  <div class="stat"><b id="total">0</b>মোট</div>
  <div class="stat"><b id="remaining">0</b>বাকি</div>
  <div class="stat"><b id="eta">-</b>ETA</div>
</div>
<div id="status-msg">প্রসেসিং শুরু হচ্ছে...</div>
</div>
<script>
const jobId = "%s";
function fmtBytes(n){
  n = n || 0;
  const units = ["B","KB","MB","GB"];
  let i = 0;
  while(n >= 1024 && i < units.length-1){ n /= 1024; i++; }
  return (i===0? Math.round(n) : n.toFixed(1)) + units[i];
}
function fmtEta(s){
  if(s<=0) return "0s";
  if(s<60) return s+"s";
  return Math.floor(s/60)+"m "+(s%%60)+"s";
}
async function poll(){
  try{
    const r = await fetch("/api/mhtml-status/"+jobId);
    const d = await r.json();
    if(d.error){
      document.getElementById("status-msg").textContent = "❌ " + d.error;
      return;
    }
    const remaining = Math.max(0, (d.total||0) - (d.done||0));
    document.getElementById("bar").style.width = d.pct + "%%";
    document.getElementById("bar").textContent = d.pct + "%%";
    if(d.phase === "downloading"){
      document.getElementById("done").textContent = fmtBytes(d.dl_done);
      document.getElementById("total").textContent = d.dl_total ? fmtBytes(d.dl_total) : "?";
      document.getElementById("remaining").textContent = d.dl_speed ? fmtBytes(d.dl_speed)+"/s" : "-";
    } else {
      document.getElementById("done").textContent = d.done;
      document.getElementById("total").textContent = d.total || "?";
      document.getElementById("remaining").textContent = remaining;
    }
    document.getElementById("eta").textContent = fmtEta(d.eta_sec);
    if(d.status === "done"){
      document.getElementById("status-msg").textContent = "✅ সম্পন্ন! (" + (d.source||"") + ") — CSV Telegram-এ পাঠানো হয়েছে।";
      return;
    }
    if(d.status === "error"){
      document.getElementById("status-msg").textContent = "❌ Error: " + (d.error||"unknown");
      return;
    }
    const phaseLabels = {
      "downloading": "📥 File ডাউনলোড হচ্ছে...",
      "detecting": "🔍 Format যাচাই করা হচ্ছে...",
      "parsing": "⏳ MCQ প্রসেসিং চলছে...",
      "csv_building": "📄 CSV বানানো হচ্ছে...",
      "sending": "📤 CSV পাঠানো হচ্ছে..."
    };
    document.getElementById("status-msg").textContent = phaseLabels[d.phase] || "⏳ প্রসেসিং চলছে...";
    setTimeout(poll, 1500);
  }catch(e){
    setTimeout(poll, 3000);
  }
}
poll();
</script>
</body></html>""" % job_id
    return HTMLResponse(content=html)


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
        new_mcqs = _cap_mcq_options(await generate_new_mcq(img, cache["topic"], cache["page_number"], mcq_count=15))
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
        new_mcqs = _cap_mcq_options(await generate_new_mcq(img, cache["topic"], cache["page_number"], mcq_count=15))
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

async def _scheduled_restart_task() -> None:
    """v-RAM-fix: clean self-exit every 12h so Render restarts the process
    fresh, fully resetting RAM regardless of any leak."""
    await asyncio.sleep(12 * 3600)
    logger.info("[Restart] Scheduled restart: exiting cleanly for fresh RAM")
    os._exit(0)


async def _memory_cleanup_task() -> None:
    """v-RAM-fix: periodic gc + re-enforce cache caps every 30 min, so leaks
    never accumulate over days/weeks/months even if a cap write is missed."""
    import gc
    await asyncio.sleep(300)
    while True:
        try:
            for attr in ("pdf_cache", "qbm_cache", "img_cache"):
                cache = getattr(app.state, attr, None)
                if cache is not None:
                    _cap_page_cache(cache)
            gc.collect()
            logger.info(
                f"[MemCleanup] pdf_cache={len(getattr(app.state,'pdf_cache',{}) or {})} "
                f"qbm_cache={len(getattr(app.state,'qbm_cache',{}) or {})} "
                f"img_cache={len(getattr(app.state,'img_cache',{}) or {})}"
            )
        except Exception as e:
            logger.warning(f"[MemCleanup] {e}")
        await asyncio.sleep(1800)


_active_jobs = {"count": 0}

async def _ram_guard_task() -> None:
    """Proactive RSS watchdog. Was tuned for 512MB Render free tier; now on
    16GB HF Space, threshold raised proportionally so restarts only trigger
    on genuine leaks, not normal heavy-load usage."""
    try:
        import psutil
    except ImportError:
        logger.warning("[RAMGuard] psutil not installed -> proactive RAM guard disabled")
        return
    proc = psutil.Process(os.getpid())
    limit_mb = 14000  # 16GB instance, ~14GB usable ceiling (leave OS/runtime headroom)
    threshold_mb = int(limit_mb * 0.75)
    await asyncio.sleep(60)
    while True:
        try:
            rss_mb = proc.memory_info().rss / (1024 * 1024)
            hard_cap_mb = int(limit_mb * 0.88)
            if rss_mb >= hard_cap_mb:
                logger.warning(f"[RAMGuard] RSS {rss_mb:.0f}MB >= hard cap {hard_cap_mb}MB -> forced restart (job or not)")
                await asyncio.sleep(1)
                os._exit(0)
            if rss_mb >= threshold_mb:
                if _active_jobs["count"] > 0:
                    logger.warning(f"[RAMGuard] RSS {rss_mb:.0f}MB >= threshold but {_active_jobs['count']} job(s) active -> deferring restart")
                else:
                    logger.warning(f"[RAMGuard] RSS {rss_mb:.0f}MB >= {threshold_mb}MB threshold -> clean self-restart")
                    await asyncio.sleep(1)
                    os._exit(0)
            elif rss_mb >= threshold_mb * 0.9:
                logger.info(f"[RAMGuard] RSS {rss_mb:.0f}MB approaching threshold ({threshold_mb}MB)")
        except Exception as e:
            logger.warning(f"[RAMGuard] check failed: {e}")
        await asyncio.sleep(60)


async def _keepalive_task() -> None:
    """Self-ping own service URL /health every 5 min for 24/7 uptime
    (prevents platform sleep). Alerting disabled — AtlasBot handles
    monitoring/alerts now."""
    await asyncio.sleep(60)
    logger.info("[App] Keep-alive task started")
    while True:
        if RENDER_URL:
            try:
                async with httpx.AsyncClient(timeout=40) as client:
                    await client.get(f"{RENDER_URL.rstrip('/')}/health")
            except Exception:
                pass
        await asyncio.sleep(300)


async def _watchdog_task() -> None:
    """Independent watchdog — separate ping loop (offset timing) that
    double-checks the bot is alive. If keep-alive silently dies (task
    crash) this loop still detects downtime and wakes/alerts."""
    await asyncio.sleep(150)  # offset from _keepalive_task so they don't overlap
    logger.info("[App] Watchdog task started")
    fails = 0
    was_down = False
    while True:
        healthy = False
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                if RENDER_URL:
                    r = await client.get(f"{RENDER_URL.rstrip('/')}/health")
                    healthy = r.status_code == 200
        except Exception:
            healthy = False

        if healthy:
            fails = 0
            if was_down:
                try:
                    await notify_owner("✅ QuizBot WATCHDOG: service reachable again.")
                except Exception:
                    pass
                was_down = False
        else:
            fails += 1
            logger.warning(f"[Watchdog] health check failed ({fails} in a row)")
            if fails >= 4:
                if not was_down:
                    try:
                        await notify_owner(f"🚨 QuizBot WATCHDOG: service unreachable ({fails}x) — attempting self-wake.")
                    except Exception:
                        pass
                    was_down = True
                # self-wake attempt: hit health endpoint directly again
                try:
                    async with httpx.AsyncClient(timeout=30) as client:
                        if RENDER_URL:
                            await client.get(f"{RENDER_URL.rstrip('/')}/health")
                except Exception:
                    pass
        await asyncio.sleep(300)


async def _watchdog2_task() -> None:
    """3rd independent ping layer — different offset/interval than keep-alive
    and watchdog, so all three never crash/miss at the same moment."""
    await asyncio.sleep(240)
    logger.info("[App] Watchdog-2 task started")
    fails = 0
    was_down2 = False
    while True:
        healthy = False
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                if RENDER_URL:
                    r = await client.get(f"{RENDER_URL.rstrip('/')}/health")
                    healthy = r.status_code == 200
        except Exception:
            healthy = False
        if healthy:
            fails = 0
            if was_down2:
                try:
                    await notify_owner("✅ QuizBot WATCHDOG-2: service reachable again.")
                except Exception:
                    pass
                was_down2 = False
        else:
            fails += 1
            if fails >= 4:
                if not was_down2:
                    try:
                        await notify_owner(f"🚨 QuizBot WATCHDOG-2: unreachable ({fails}x) — self-wake attempt.")
                    except Exception:
                        pass
                    was_down2 = True
                if RENDER_URL:
                    for _ in range(2):
                        try:
                            async with httpx.AsyncClient(timeout=30) as client:
                                await client.get(f"{RENDER_URL.rstrip('/')}/health")
                            break
                        except Exception:
                            await asyncio.sleep(5)
        await asyncio.sleep(420)


async def _cross_bot_watchdog_task() -> None:
    """Mutual watchdog: pings AtlasBot's health endpoint (set via
    ATLASBOT_URL env). If AtlasBot looks down, alerts owner — mirrors
    AtlasBot's own cross-check on this bot."""
    atlasbot_url = os.environ.get("ATLASBOT_URL", "").rstrip("/")
    if not atlasbot_url:
        return
    await asyncio.sleep(200)
    logger.info("[App] Cross-bot watchdog (-> AtlasBot) started")
    fails = 0
    while True:
        healthy = False
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                r = await client.get(f"{atlasbot_url}/health")
                healthy = r.status_code == 200
        except Exception:
            healthy = False
        if healthy:
            fails = 0
        else:
            fails += 1
            if fails >= 4:
                try:
                    await notify_owner(f"🚨 AtlasBot unreachable via cross-bot check ({fails}x) — checked from QuizBot.")
                except Exception:
                    pass
        await asyncio.sleep(300)


@app.get("/health")
async def health():
    return {"status": "ok", "db": sb is not None, "gemini_keys": len(key_rotator.keys), "bot_token": bool(BOT_TOKEN)}

@app.on_event("startup")
async def startup():
    global BOT_START_TIME
    BOT_START_TIME = time.time()
    logger.info("[App] ATLAS BOT v4.1 starting...")

    # Start mhtml/html auto-queue worker (serial processing, one file at a time)
    global _mhtml_worker_started
    if not _mhtml_worker_started:
        asyncio.create_task(_mhtml_auto_worker())
        _mhtml_worker_started = True
        logger.info("[App] mhtml auto-queue worker started")

    async def _supervised(coro_fn, name):
        """Core background task crash korle silently die na kore auto-restart hobe.
        Exponential backoff + alert-spam prevent (repeated crash e max 3 alert)."""
        fail_count = 0
        while True:
            try:
                await coro_fn()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                fail_count += 1
                wait = min(10 * (2 ** min(fail_count - 1, 5)), 300)
                logger.error(f"⚠️ [Supervisor] {name} crashed ({fail_count}x): {e} — restarting in {wait}s")
                if fail_count <= 3:
                    try:
                        await send_msg(OWNER_ID, f"⚠️ Background task '{name}' crashed ({fail_count}x), auto-restarting: {e}")
                    except Exception:
                        pass
                await asyncio.sleep(wait)

    # Self-ping keep-alive: prevents platform sleep. Watchdog alert tasks
    # disabled per request — AtlasBot now handles all external monitoring.
    asyncio.create_task(_supervised(_keepalive_task, "_keepalive_task"))
    asyncio.create_task(_supervised(_memory_cleanup_task, "_memory_cleanup_task"))
    asyncio.create_task(_supervised(_ram_guard_task, "_ram_guard_task"))
    asyncio.create_task(_supervised(_scheduled_restart_task, "_scheduled_restart_task"))
    # asyncio.create_task(_supervised(_watchdog_task, "_watchdog_task"))  # DISABLED — AtlasBot monitors instead
    # asyncio.create_task(_supervised(_watchdog2_task, "_watchdog2_task"))  # DISABLED — AtlasBot monitors instead
    # asyncio.create_task(_supervised(_cross_bot_watchdog_task, "_cross_bot_watchdog_task"))  # DISABLED

    if not BOT_TOKEN:
        logger.error("[App] BOT_TOKEN missing!")
        return
    logger.info("[App] Using CF Worker proxy for TG API")

    # ── Auto webhook set ──
    # নিয়ম: Render শুধু webhook নিজের দিকে নেবে যদি বর্তমানে webhook
    # ইতিমধ্যেই Render-এ set থাকে (restart এর পর হারিয়ে না যায়) —
    # HF সচল থাকলে HF থেকে কেড়ে নেবে না, CF cron-ই auto-switch handle করে।
    try:
        import httpx as _hx, os as _os
        running_on = _os.environ.get("RUNNING_ON", "") or "HuggingFace Space"
        self_url = RENDER_URL or ""

        if running_on == "Render" or (self_url and "onrender.com" in self_url):
            webhook_url = self_url.rstrip("/") + "/webhook"
            async with _hx.AsyncClient(timeout=10) as _c:
                info_r = await _c.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo")
                current_url = info_r.json().get("result", {}).get("url", "")

                if "onrender.com" in current_url:
                    # ইতিমধ্যেই Render-এ ছিল — restart এর পর re-confirm করছি
                    _r_payload = {"url": webhook_url, "drop_pending_updates": False, "max_connections": 40}
                    if WEBHOOK_SECRET:
                        _r_payload["secret_token"] = WEBHOOK_SECRET
                    r = await _c.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                        json=_r_payload
                    )
                    result = r.json()
                    if result.get("ok"):
                        logger.info(f"[App] ✅ Render webhook re-confirmed → {webhook_url}")
                    else:
                        logger.warning(f"[App] Render webhook failed: {result.get('description')}")
                else:
                    logger.info(f"[App] Webhook currently on '{current_url}' (not Render) — leaving as-is, CF cron handles failover")
        else:
            # HF তে আছি — webhook CF Worker URL-এ থাকা উচিত, প্রতি startup-এ verify+force-set করি
            # (direct api.telegram.org call HF-এ blocked, tai tg_post() proxy babohar)
            worker_webhook = CF_WORKER_URL.rstrip("/") + "/webhook"
            info_r = await tg_post("getWebhookInfo", {})
            current_url = info_r.get("result", {}).get("url", "")

            if current_url != worker_webhook or WEBHOOK_SECRET:
                _wh_payload = {"url": worker_webhook, "drop_pending_updates": False, "max_connections": 40}
                if WEBHOOK_SECRET:
                    _wh_payload["secret_token"] = WEBHOOK_SECRET
                result = await tg_post("setWebhook", _wh_payload)
                if result.get("ok"):
                    logger.info(f"[App] ✅ HF: webhook set → {worker_webhook} (secret={'yes' if WEBHOOK_SECRET else 'no'})")
                else:
                    logger.warning(f"[App] HF: webhook correction failed: {result.get('description')}")
            else:
                logger.info(f"[App] HF: webhook already correct → {worker_webhook}")
    except Exception as e:
        logger.error(f"[App] Webhook setup error: {e}")

    try:
        ok, admin_ok, admin_total = await set_bot_commands()
        logger.info(f"[App] Command menu set on startup: default={ok}, admins={admin_ok}/{admin_total}")
    except Exception as e:
        logger.error(f"[App] Failed to set command menu on startup: {e}")
    try:
        await _recover_rapid_jobs()
    except Exception as e:
        logger.error(f"[App] /rapid job recovery failed: {e}")


