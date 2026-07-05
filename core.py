# ============================================================
# ATLAS BOT — Core Shared Infrastructure
# Config, D1 (Cloudflare) helpers, Telegram API helpers,
# Supabase helpers, FastAPI app instance.
# Imported by both app.py and quiz.py — no circular dependency.
# ============================================================

import os
import json
import logging
import time
import base64
from typing import Optional

import httpx
from fastapi import FastAPI
from supabase import create_client

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","module":"%(name)s","msg":"%(message)s"}'
)
logger = logging.getLogger("atlas.core")

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "https://atlasquizbotpro.hamza818483.workers.dev")
HF_SPACE_URL = os.environ.get("HF_SPACE_URL", "https://quizbot-s482.onrender.com")  # v4.2: HF permanently banned, Render is primary
RENDER_URL = os.environ.get("RENDER_URL", "")
D1_TOKEN = os.environ.get("D1_TOKEN", "")
# v4.3: GitHub Pages exam link — CF down thakleo page load hoy (static host),
# er bhitorer JS nijei Render->CF->Supabase try kore. Beshi robust than CF-hosted /exam/.
GH_PAGES_EXAM_URL = os.environ.get("GH_PAGES_EXAM_URL", "https://hamza818483-dotcom.github.io/QuizBot/exam.html")

# Render এ চললে directly TG API, HF এ চললে CF proxy (HF তে TG blocked)
_running_on = os.environ.get("RUNNING_ON", "")
if _running_on == "Render" or (RENDER_URL and "onrender.com" in RENDER_URL):
    TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
    _tg_mode = "direct"
else:
    TG_API = f"{CF_WORKER_URL}/tg-proxy"
    _tg_mode = "cf-proxy"

logger.info(f"[Core] TG API mode: {_tg_mode}")

# ============================================================
# SUPABASE CLIENT
# ============================================================
import httpx as _httpx

try:
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("[DB] Supabase connected")
except Exception as e:
    logger.error(f"[DB] Supabase connection failed: {e}")
    sb = None

def _patch_supabase_execute_with_retry():
    """v1.1: monkey-patch postgrest's execute() so any sb.table(...)...execute()
    call auto-retries once on transient HTTP/2 ConnectionTerminated errors.
    The underlying connection can be closed server-side after being idle
    (load balancer / idle timeout); recreating the global Supabase client
    opens a fresh connection. This requires zero changes at any of the
    existing sb.table(...) call sites across the codebase."""
    try:
        from postgrest._sync.request_builder import SyncQueryRequestBuilder
    except ImportError:
        logger.warning("[DB] Could not patch postgrest execute() — retry-on-disconnect disabled")
        return

    original_execute = SyncQueryRequestBuilder.execute

    def patched_execute(self):
        try:
            return original_execute(self)
        except (_httpx.RemoteProtocolError, _httpx.ConnectError, _httpx.ReadError) as e:
            global sb
            logger.warning(f"[DB] Supabase connection error ({type(e).__name__}) — recreating client and retrying once")
            try:
                sb = create_client(SUPABASE_URL, SUPABASE_KEY)
            except Exception as ce:
                logger.error(f"[DB] Supabase client recreation failed: {ce}")
            return original_execute(self)

    SyncQueryRequestBuilder.execute = patched_execute

_patch_supabase_execute_with_retry()

# ============================================================
# FASTAPI APP (single shared instance)
# ============================================================
app = FastAPI(title="ATLAS BOT", version="4.2.0")

# ============================================================
# D1 (CLOUDFLARE) HELPERS
# ============================================================

# ── In-memory KV fallback (used when CF Worker is down) ──
_mem_kv: dict = {}

async def d1_set(key: str, value: dict, ttl: int = 86400):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{CF_WORKER_URL}/d1/set",
                json={"key": key, "value": value, "ttl": ttl})
            if r.text.strip():
                ok = r.json().get("ok", False)
                if ok:
                    _mem_kv[key] = value  # mirror to memory
                    return True
        return True
    except Exception as e:
        logger.warning(f"[D1] set warn (using memory): {e}")
        _mem_kv[key] = value  # fallback: RAM
        return True

async def d1_get(key: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{CF_WORKER_URL}/d1/get", params={"key": key})
            if r.text.strip():
                data = r.json()
                val = data.get("value")
                if val is not None:
                    _mem_kv[key] = val  # mirror
                    return val
        return _mem_kv.get(key)  # fallback: RAM
    except Exception as e:
        logger.warning(f"[D1] get warn (using memory): {e}")
        return _mem_kv.get(key)

async def d1_del(key: str):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"{CF_WORKER_URL}/d1/del", json={"key": key})
    except Exception as e:
        logger.warning(f"[D1] del warn: {e}")
    _mem_kv.pop(key, None)  # always clean memory too


async def d1_query(sql: str, params: list = None, is_select: bool = True) -> dict:
    try:
        body = {"sql": sql, "params": params or [], "token": D1_TOKEN}
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{CF_WORKER_URL}/d1/query", json=body)
            data = r.json()
            if not data.get("ok"):
                logger.warning(f"[D1] query error: {data.get('error')}")
                return {"ok": False, "error": data.get("error")}
            return data
    except Exception as e:
        logger.warning(f"[D1] query error: {e}")
        return {"ok": False, "error": str(e)}

async def d1_select(sql: str, params: list = None) -> list:
    r = await d1_query(sql, params, True)
    if r.get("ok") and r.get("results") is not None:
        return r.get("results", [])
    # ── CF down → Supabase direct fallback ──
    try:
        if "quizzes" in sql.lower() and params:
            qid = params[0]
            async with httpx.AsyncClient(timeout=10) as c:
                rr = await c.get(f"{SUPABASE_URL}/rest/v1/quiz_backups",
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                    params={"quiz_id": f"eq.{qid}", "select": "*"})
            data = rr.json()
            if data and data[0]:
                b = data[0]
                import json as _json
                return [{
                    "id": b["quiz_id"], "name": b.get("name", "Special Topic"),
                    "description": "", "timer": 30, "shuffle": 0,
                    "csv_data": _json.dumps(b.get("questions", [])),
                    "tag": "", "exp_footer": "", "created_by": b.get("created_by", 0),
                }]
    except Exception as e:
        logger.warning(f"[D1] Supabase fallback failed: {e}")
    return []

async def d1_run(sql: str, params: list = None) -> bool:
    r = await d1_query(sql, params, False)
    if r.get("ok"):
        return True
    # ── CF down → Supabase fallback for quiz INSERT/REPLACE ──
    try:
        sql_lower = sql.lower()
        if "quizzes" in sql_lower and ("insert" in sql_lower or "replace" in sql_lower) and params and len(params) >= 9:
            import json as _j
            qs = params[5]
            questions = _j.loads(qs) if isinstance(qs, str) else qs
            payload = {
                "quiz_id": params[0], "name": params[1],
                "questions": questions, "created_by": params[8] or 0,
            }
            headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                       "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}
            async with httpx.AsyncClient(timeout=10) as c:
                rr = await c.post(f"{SUPABASE_URL}/rest/v1/quiz_backups",
                                  headers=headers, json=payload)
            logger.info(f"[D1] Supabase write fallback: {rr.status_code}")
            return rr.status_code in (200, 201, 204)
    except Exception as e:
        logger.warning(f"[D1] Supabase write fallback failed: {e}")
    return False

# ============================================================
# /error COMMAND — SIMPLE FILE-BASED ERROR CAPTURE (AtlasBot-style)
# Every logger.error(...) call anywhere in the codebase is automatically
# captured here (no need to touch existing try/except blocks) and appended
# to a plain daily local log file for the /error command to tail. Replaces
# the previous D1 (Cloudflare)-backed structured logging system.
# ============================================================
import traceback as _traceback
from datetime import datetime as _datetime
import pytz as _pytz

BD_TZ = _pytz.timezone("Asia/Dhaka")
LOG_DIR = os.environ.get("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def _error_log_path() -> str:
    return os.path.join(LOG_DIR, f"errors_{_datetime.now(BD_TZ).strftime('%Y-%m-%d')}.log")


def _append_error_log(record: logging.LogRecord):
    try:
        timestamp = _datetime.now(BD_TZ).strftime("%Y-%m-%d %H:%M:%S")
        tb = ""
        if record.exc_info:
            tb = "".join(_traceback.format_exception(*record.exc_info))
        with open(_error_log_path(), "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {record.getMessage()}\n{tb}{'='*50}\n")
    except Exception:
        pass  # never let error logging itself crash the bot


class _FileErrorCaptureHandler(logging.Handler):
    """Captures every logger.error()/logger.exception() call and appends it
    to today's local log file, without blocking the caller."""
    def emit(self, record: logging.LogRecord):
        if record.levelno < logging.ERROR:
            return
        try:
            _append_error_log(record)
        except Exception:
            pass  # logging must never raise


logging.getLogger().addHandler(_FileErrorCaptureHandler())


async def get_recent_errors(limit: int = 10) -> str:
    """Used by the /error command — returns the tail of today's plain-text
    error log file (AtlasBot-style), or '' if no errors logged today."""
    path = _error_log_path()
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return content.strip()
    except Exception:
        return ""


async def clear_error_logs():
    """Deletes today's error log file (AtlasBot-style /error clear)."""
    path = _error_log_path()
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ============================================================
# TELEGRAM HELPERS
# ============================================================
async def tg_post(method: str, data: dict) -> dict:
    # ── Primary: CF Worker TG proxy ──
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{TG_API}/{method}", json=data)
            result = r.json()
            if result.get("ok"):
                return result
            logger.warning(f"[TG] {method} proxy failed: {result.get('description')}")
    except Exception as e:
        logger.warning(f"[TG] {method} proxy error: {e}")
    # ── Fallback: Direct Telegram API (CF down হলেও কাজ করবে) ──
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=data)
            result = r.json()
            if not result.get("ok"):
                logger.warning(f"[TG] {method} direct failed: {result.get('description')}")
            return result
    except Exception as e:
        logger.error(f"[TG] {method} direct error: {e}")
        return {"ok": False, "error": str(e)}

# ============================================================
# BOT USERNAME — cached, real username via getMe() (never hardcode)
# ============================================================
_BOT_USERNAME_CACHE = {"value": None}

async def get_bot_username() -> str:
    """Real bot username getMe() diye fetch kore, process lifetime cache kore.
    Deep-link (Quiz Solve/Poll Solve/Premium PDF) URL banate always ei
    function use korte hobe — kokhono hardcode username diye na."""
    if _BOT_USERNAME_CACHE["value"]:
        return _BOT_USERNAME_CACHE["value"]
    try:
        info = await tg_post("getMe", {})
        uname = info.get("result", {}).get("username")
        if uname:
            _BOT_USERNAME_CACHE["value"] = uname
            return uname
    except Exception as e:
        logger.error(f"[BotUsername] getMe failed: {e}")
    return "atlasQuizProBot"  # last-resort fallback only

async def send_msg(chat_id, text: str, parse_mode: str = "HTML",
                   reply_markup=None, reply_to_message_id: int = None) -> dict:
    data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        data["reply_markup"] = reply_markup
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    return await tg_post("sendMessage", data)

async def edit_msg(chat_id, message_id: int, text: str, parse_mode: str = "HTML") -> dict:
    return await tg_post("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode
    })

async def send_photo(chat_id, photo_bytes: bytes, caption: str = "",
                     reply_markup=None, reply_to_message_id: int = None,
                     message_thread_id: int = None) -> dict:
    # ── Primary: CF Worker (b64 proxy) ──
    try:
        b64 = base64.b64encode(photo_bytes).decode()
        data = {"chat_id": str(chat_id), "caption": caption, "photo_b64": b64}
        if reply_markup: data["reply_markup"] = reply_markup
        if reply_to_message_id: data["reply_to_message_id"] = reply_to_message_id
        if message_thread_id: data["message_thread_id"] = message_thread_id
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{CF_WORKER_URL}/tg-sendphoto", json=data)
            result = r.json()
            if result.get("ok"): return result
    except Exception as e:
        logger.warning(f"[TG] sendPhoto CF failed: {e}")
    # ── Fallback: Direct TG API multipart (CF down হলে) ──
    try:
        fields = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
        if reply_to_message_id: fields["reply_to_message_id"] = str(reply_to_message_id)
        if message_thread_id: fields["message_thread_id"] = str(message_thread_id)
        if reply_markup:
            import json as _j
            fields["reply_markup"] = _j.dumps(reply_markup)
        files = {"photo": ("photo.jpg", photo_bytes, "image/jpeg")}
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data=fields, files=files)
            return r.json()
    except Exception as e:
        logger.error(f"[TG] sendPhoto direct failed: {e}")
        return {"ok": False, "error": str(e)}

async def send_photo_by_id(chat_id, file_id: str, caption: str = "",
                           parse_mode: str = "HTML") -> dict:
    return await tg_post("sendPhoto", {
        "chat_id": chat_id,
        "photo": file_id,
        "caption": caption,
        "parse_mode": parse_mode
    })

async def send_document(chat_id, file_bytes: bytes, filename: str,
                        caption: str = "", mime_type="application/octet-stream",
                        reply_to_message_id: int = None, parse_mode: str = "HTML") -> dict:
    # ── Primary: CF Worker (b64 proxy) ──
    try:
        data = {
            "chat_id": str(chat_id), "caption": caption, "parse_mode": parse_mode,
            "filename": filename, "mime_type": mime_type,
            "doc_b64": base64.b64encode(file_bytes).decode()
        }
        if reply_to_message_id: data["reply_to_message_id"] = reply_to_message_id
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{CF_WORKER_URL}/tg-senddoc", json=data)
            result = r.json()
            if result.get("ok"): return result
    except Exception as e:
        logger.warning(f"[sendDoc] CF failed: {e}")
    # ── Fallback: Direct TG API multipart ──
    try:
        fields = {"chat_id": str(chat_id), "caption": caption, "parse_mode": parse_mode}
        if reply_to_message_id: fields["reply_to_message_id"] = str(reply_to_message_id)
        files = {"document": (filename, file_bytes, mime_type)}
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data=fields, files=files)
            return r.json()
    except Exception as e:
        logger.error(f"[sendDoc] direct failed: {e}")
        return {"ok": False, "error": str(e)}

async def send_poll(chat_id, question: str, options: list, correct_idx: int,
                    explanation: str = "", reply_to_message_id: int = None,
                    message_thread_id: int = None) -> dict:
    data = {
        "chat_id": chat_id,
        "question": question[:300],
        "options": [o[:100] for o in options],
        "type": "quiz",
        "correct_option_id": correct_idx,
        "is_anonymous": True,
        "explanation": explanation[:200]
    }
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    if message_thread_id:
        data["message_thread_id"] = message_thread_id
    return await tg_post("sendPoll", data)

async def notify_owner(text: str):
    if OWNER_ID:
        await send_msg(OWNER_ID, f"🔔 <b>ATLAS BOT Alert</b>\n\n{text}")

# ============================================================
# DOWNLOAD FILE VIA CF PROXY
# ============================================================
async def download_tg_file(file_id: str, progress_cb=None) -> bytes:
    file_res = await tg_post("getFile", {"file_id": file_id})
    if not file_res.get("ok"):
        raise Exception(f"getFile failed: {file_res.get('description')}")
    file_path = file_res["result"]["file_path"]
    total_size = file_res["result"].get("file_size", 0)

    async def _stream_download(url: str) -> bytes:
        chunks = []
        downloaded = 0
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("GET", url) as r:
                if r.status_code != 200:
                    raise Exception(f"HTTP {r.status_code}")
                async for chunk in r.aiter_bytes(chunk_size=262144):
                    chunks.append(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        try:
                            progress_cb(downloaded, total_size)
                        except Exception:
                            pass
        return b"".join(chunks)

    try:
        return await _stream_download(f"{CF_WORKER_URL}/tg-file?path={file_path}")
    except Exception as e:
        logger.warning(f"[Download] CF proxy file failed: {e}")
    return await _stream_download(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}")

# ============================================================
# SUPABASE HELPERS (shared)
# ============================================================
async def db_get_settings() -> dict:
    try:
        r = sb.table("quiz_settings").select("tag,exp_footer").eq("id", 1).execute()
        if r.data:
            return r.data[0]
    except Exception as e:
        logger.error(f"[DB] get_settings error: {e}")
    return {"tag": "", "exp_footer": ""}

async def db_is_owner_or_admin(uid: int) -> bool:
    if uid == OWNER_ID:
        return True
    try:
        r = sb.table("admins").select("user_id").eq("user_id", uid).execute()
        return len(r.data) > 0
    except:
        return False

async def db_track_user(uid: int, uname: str):
    try:
        sb.table("pdf_users").upsert({
            "user_id": uid, "user_name": uname, "last_seen": int(time.time())
        }).execute()
    except Exception as e:
        logger.error(f"[DB] track_user error: {e}")

async def db_save_session(session_id: str, data: dict):
    try:
        sb.table("pdf_sessions").upsert({"id": session_id, **data}).execute()
    except Exception as e:
        logger.error(f"[DB] save_session error: {e}")

async def db_save_mcq_cache(cache_id: str, session_id: str, page: int,
                             topic: str, mcqs: list, poll_links: list = None,
                             image_file_id: str = None, image_msg_id: int = None,
                             channel_id: str = None, is_new_gen: bool = False,
                             end_msg_id: int = None):
    try:
        sb.table("pdf_mcq_cache").upsert({
            "id": cache_id, "session_id": session_id, "page_number": page,
            "topic": topic, "mcq_data": mcqs, "poll_links": poll_links or [],
            "image_file_id": image_file_id, "image_msg_id": image_msg_id,
            "channel_id": channel_id or "", "is_new_gen": is_new_gen,
            "end_msg_id": end_msg_id, "new_gen_count": 0
        }).execute()
    except Exception as e:
        logger.error(f"[DB] save_mcq_cache error: {e}")

async def db_update_cache(cache_id: str, fields: dict):
    try:
        sb.table("pdf_mcq_cache").update(fields).eq("id", cache_id).execute()
    except Exception as e:
        logger.error(f"[DB] update_cache error: {e}")

async def db_get_mcq_cache(cache_id: str) -> dict:
    try:
        r = sb.table("pdf_mcq_cache").select("*").eq("id", cache_id).execute()
        if r.data:
            return r.data[0]
    except Exception as e:
        logger.error(f"[DB] get_mcq_cache error: {e}")
    return None

async def db_get_new_gen_count(cache_id: str, user_id: int) -> int:
    try:
        r = sb.table("new_gen_count").select("count")\
            .eq("cache_id", cache_id).eq("user_id", user_id).execute()
        if r.data:
            return r.data[0]["count"]
    except:
        pass
    return 0

async def db_increment_gen_count(cache_id: str, user_id: int) -> int:
    try:
        count = await db_get_new_gen_count(cache_id, user_id) + 1
        sb.table("new_gen_count").upsert({
            "cache_id": cache_id, "user_id": user_id,
            "count": count, "updated_at": int(time.time())
        }).execute()
        return count
    except Exception as e:
        logger.error(f"[DB] increment_gen_count error: {e}")
        return 0

async def db_save_leaderboard(cache_id: str, user_id: int, user_name: str,
                               topic: str, page: int, correct: int,
                               total: int, final_score: float):
    try:
        r = sb.table("web_exam_leaderboard").select("final_score")\
            .eq("cache_id", cache_id).eq("user_id", user_id).execute()
        if r.data:
            if final_score > r.data[0]["final_score"]:
                sb.table("web_exam_leaderboard").update({
                    "user_name": user_name, "correct": correct,
                    "total": total, "final_score": final_score,
                    "updated_at": int(time.time())
                }).eq("cache_id", cache_id).eq("user_id", user_id).execute()
        else:
            sb.table("web_exam_leaderboard").insert({
                "cache_id": cache_id, "user_id": user_id, "user_name": user_name,
                "topic": topic, "page_number": page, "correct": correct,
                "total": total, "final_score": final_score
            }).execute()
    except Exception as e:
        logger.error(f"[DB] save_leaderboard error: {e}")

async def db_get_channels() -> list:
    try:
        r = sb.table("channels").select("*").execute()
        return r.data or []
    except:
        return []

# ============================================================
# QUIZ STATE (last-quiz resume, shared by image/pdf quiz solve)
# ============================================================
async def db_save_last_quiz(uid: int, st: dict):
    try:
        sb.table("quiz_last_state").upsert({
            "user_id": uid, "cache_id": st["cache_id"],
            "topic": st.get("topic", ""), "page_number": st.get("page", 1),
            "mcqs": st.get("mcqs", []), "wrong_idx": st.get("wrong_idx", []),
            "skip_idx": st.get("skip_idx", []), "src_indices": st.get("src_indices"),
            "channel_id": st.get("channel_id", ""), "back_msg_id": st.get("back_msg_id"),
            "is_new_gen": bool(st.get("is_new_gen")), "right_count": st.get("right", 0),
            "wrong_count": st.get("wrong", 0), "skip_count": st.get("skip", 0),
            "uname": st.get("uname", ""), "updated_at": int(time.time())
        }).execute()
    except Exception as e:
        logger.error(f"[DB] save_last_quiz error: {e}")

async def db_get_last_quiz(uid: int) -> dict:
    try:
        r = sb.table("quiz_last_state").select("*").eq("user_id", uid).execute()
        if r.data:
            row = r.data[0]
            return {
                "cache_id": row["cache_id"], "topic": row["topic"],
                "page": row["page_number"], "mcqs": row["mcqs"],
                "wrong_idx": row["wrong_idx"] or [], "skip_idx": row["skip_idx"] or [],
                "src_indices": row["src_indices"], "channel_id": row["channel_id"] or "",
                "back_msg_id": row["back_msg_id"], "is_new_gen": bool(row["is_new_gen"]),
                "right": row["right_count"], "wrong": row["wrong_count"],
                "skip": row["skip_count"], "uname": row["uname"] or "",
            }
    except Exception as e:
        logger.error(f"[DB] get_last_quiz error: {e}")
    return None

# ============================================================
# SHARED HELPERS
# ============================================================
def build_back_url(channel_id, msg_id) -> Optional[str]:
    if not channel_id:
        return None
    cid = str(channel_id)
    if cid.startswith("-100"):
        c = cid[4:]
        return f"https://t.me/c/{c}/{msg_id}" if msg_id else f"https://t.me/c/{c}"
    c = cid.lstrip("@")
    return f"https://t.me/{c}/{msg_id}" if msg_id else f"https://t.me/{c}"

def source_msg_id(cache: dict):
    return cache.get("end_msg_id") or cache.get("image_msg_id")

# ============================================================
# WATERMARK (ported from AtlasMasterBot's services.py)
# ============================================================
def add_watermark_to_pdf(pdf_bytes: bytes, watermark_text: str) -> bytes:
    """Add a diagonal, semi-transparent text watermark to every page of a PDF."""
    try:
        import io as _io
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas
        from reportlab.lib.colors import Color

        reader = PdfReader(_io.BytesIO(pdf_bytes))
        writer = PdfWriter()

        for page in reader.pages:
            packet = _io.BytesIO()
            c = canvas.Canvas(packet, pagesize=(float(page.mediabox.width), float(page.mediabox.height)))
            c.setFont("Helvetica-Bold", 60)
            c.setFillColor(Color(0, 0, 0, alpha=0.10))
            page_width = float(page.mediabox.width)
            page_height = float(page.mediabox.height)
            c.saveState()
            c.translate(page_width / 2, page_height / 2)
            c.rotate(45)
            c.drawCentredString(0, 0, watermark_text)
            c.restoreState()
            c.save()
            packet.seek(0)
            overlay = PdfReader(packet)
            page.merge_page(overlay.pages[0])
            writer.add_page(page)

        buf = _io.BytesIO()
        writer.write(buf)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"[Watermark] error: {e}")
        return pdf_bytes

