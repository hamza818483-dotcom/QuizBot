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
HF_SPACE_URL = os.environ.get("HF_SPACE_URL", "https://hamzahf1-atlasboss.hf.space")
D1_TOKEN = os.environ.get("D1_TOKEN", "")

TG_API = f"{CF_WORKER_URL}/tg-proxy"

# ============================================================
# SUPABASE CLIENT
# ============================================================
try:
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("[DB] Supabase connected")
except Exception as e:
    logger.error(f"[DB] Supabase connection failed: {e}")
    sb = None

# ============================================================
# FASTAPI APP (single shared instance)
# ============================================================
app = FastAPI(title="ATLAS BOT", version="4.2.0")

# ============================================================
# D1 (CLOUDFLARE) HELPERS
# ============================================================
async def d1_set(key: str, value: dict, ttl: int = 86400):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{CF_WORKER_URL}/d1/set",
                json={"key": key, "value": value, "ttl": ttl})
            if r.text.strip():
                return r.json().get("ok", False)
        return True
    except Exception as e:
        logger.warning(f"[D1] set warn: {e}")
        return False

async def d1_get(key: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{CF_WORKER_URL}/d1/get", params={"key": key})
            if r.text.strip():
                data = r.json()
                return data.get("value")
        return None
    except Exception as e:
        logger.warning(f"[D1] get warn: {e}")
        return None

async def d1_del(key: str):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"{CF_WORKER_URL}/d1/del", json={"key": key})
    except Exception as e:
        logger.warning(f"[D1] del warn: {e}")

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
    return r.get("results", [])

async def d1_run(sql: str, params: list = None) -> bool:
    r = await d1_query(sql, params, False)
    return r.get("ok", False)

# ============================================================
# TELEGRAM HELPERS
# ============================================================
async def tg_post(method: str, data: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{TG_API}/{method}", json=data)
            result = r.json()
            if not result.get("ok"):
                logger.warning(f"[TG] {method} failed: {result.get('description')}")
            return result
    except Exception as e:
        logger.error(f"[TG] {method} error: {e}")
        return {"ok": False, "error": str(e)}

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
    try:
        b64 = base64.b64encode(photo_bytes).decode()
        data = {"chat_id": str(chat_id), "caption": caption, "photo_b64": b64}
        if reply_markup:
            data["reply_markup"] = reply_markup
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id
        if message_thread_id:
            data["message_thread_id"] = message_thread_id
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{CF_WORKER_URL}/tg-sendphoto", json=data)
            return r.json()
    except Exception as e:
        logger.error(f"[TG] sendPhoto error: {e}")
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
                        caption: str = "", mime_type="application/octet-stream") -> dict:
    try:
        data = {
            "chat_id": str(chat_id),
            "caption": caption,
            "filename": filename,
            "mime_type": mime_type,
            "doc_b64": base64.b64encode(file_bytes).decode()
        }
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{CF_WORKER_URL}/tg-senddoc", json=data)
            return r.json()
    except Exception as e:
        logger.error(f"[sendDoc] {e}")
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
async def download_tg_file(file_id: str) -> bytes:
    file_res = await tg_post("getFile", {"file_id": file_id})
    if not file_res.get("ok"):
        raise Exception(f"getFile failed: {file_res.get('description')}")
    file_path = file_res["result"]["file_path"]
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.get(f"{CF_WORKER_URL}/tg-file", params={"path": file_path})
            if r.status_code == 200:
                return r.content
    except Exception as e:
        logger.warning(f"[Download] CF proxy file failed: {e}")
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}")
        return r.content

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
