# ============================================================
# ATLAS BOT — Core Shared Infrastructure
# Config, D1 (Cloudflare) helpers, Telegram API helpers,
# Supabase helpers, FastAPI app instance.
# Imported by both app.py and quiz.py — no circular dependency.
# ============================================================

import os
import io
import asyncio
import tempfile
import re
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
OWNER_ID = 5341425626  # hardcoded — env var was unreliable across HF Space secrets

CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "https://atlasquizbotpro.hamza818483.workers.dev")
CF_WORKER_URL_2 = os.environ.get("CF_WORKER_URL_2", "https://quizbot.pages.dev")
HF_SPACE_URL = os.environ.get("HF_SPACE_URL", "https://hamza-02-quizbot.hf.space")
RENDER_URL = os.environ.get("RENDER_URL", "") or os.environ.get("HF_SPACE_URL", "https://hamza-02-quizbot.hf.space")
D1_TOKEN = os.environ.get("D1_TOKEN", "")
# v4.3: GitHub Pages exam link — CF down thakleo page load hoy (static host),
# er bhitorer JS nijei Render->CF->Supabase try kore. Beshi robust than CF-hosted /exam/.
GH_PAGES_EXAM_URL = os.environ.get("GH_PAGES_EXAM_URL", "https://hamza818483-dotcom.github.io/QuizBot/exam.html")

# Render এ চললে directly TG API, HF এ চললে CF proxy (HF তে TG blocked)
_running_on = os.environ.get("RUNNING_ON", "") or "HuggingFace Space"
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
    existing sb.table(...) call sites across the codebase.

    v1.2: also retries on Cloudflare edge errors (522/523/524/502/503) which
    surface as a postgrest APIError ("JSON could not be generated") rather
    than an httpx transport error, since Cloudflare returns an HTML error
    page instead of JSON when Supabase's origin is unreachable."""
    try:
        from postgrest._sync.request_builder import SyncQueryRequestBuilder
    except ImportError:
        logger.warning("[DB] Could not patch postgrest execute() — retry-on-disconnect disabled")
        return

    original_execute = SyncQueryRequestBuilder.execute

    def _is_transient(e) -> bool:
        if isinstance(e, (_httpx.RemoteProtocolError, _httpx.ConnectError, _httpx.ReadError, _httpx.TimeoutException)):
            return True
        msg = str(e)
        if "JSON could not be generated" in msg or any(c in msg for c in ("522", "523", "524", "502", "503", "Connection timed out")):
            return True
        return False

    def patched_execute(self):
        last_exc = None
        for attempt in range(3):  # initial try + 2 retries
            try:
                return original_execute(self)
            except Exception as e:
                if not _is_transient(e):
                    raise
                last_exc = e
                global sb
                logger.warning(f"[DB] Supabase transient error ({type(e).__name__}) — recreating client, attempt {attempt+1}/3")
                try:
                    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
                except Exception as ce:
                    logger.error(f"[DB] Supabase client recreation failed: {ce}")
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
        raise last_exc

    SyncQueryRequestBuilder.execute = patched_execute

_patch_supabase_execute_with_retry()

# ------------------------------------------------------------
# v1.3: run any blocking Supabase call off the event loop.
# Every `sb.table(...).execute()` call site in this codebase sits inside
# an `async def` handler but is a plain synchronous call — it blocks the
# whole FastAPI event loop (every user, every chat) for the duration of
# each DB round-trip. Wrap calls with `await sb_exec(lambda: sb.table(...)...)`
# to offload them to a worker thread and keep the bot responsive.
# ------------------------------------------------------------
async def sb_exec(fn, timeout: float = 20.0):
    """Run a synchronous Supabase call (e.g. lambda: sb.table('x').select('*').execute())
    on a worker thread so it doesn't block the event loop. Hard-capped at
    `timeout` seconds — supabase-py's underlying httpx client has no default
    timeout of its own, so a cold/slow connection could otherwise hang the
    calling command indefinitely with zero error logged (looks exactly like
    'first command does nothing, works on 2nd try')."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn), timeout=timeout)
    except asyncio.TimeoutError:
        logger.error(f"[DB] sb_exec timed out after {timeout}s")
        raise

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
        c = await _get_shared_http_client()
        r = await c.post(f"{CF_WORKER_URL}/d1/set",
            json={"key": key, "value": value, "ttl": ttl}, timeout=15)
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
        c = await _get_shared_http_client()
        r = await c.get(f"{CF_WORKER_URL}/d1/get", params={"key": key}, timeout=15)
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
        c = await _get_shared_http_client()
        await c.post(f"{CF_WORKER_URL}/d1/del", json={"key": key}, timeout=15)
    except Exception as e:
        logger.warning(f"[D1] del warn: {e}")
    _mem_kv.pop(key, None)  # always clean memory too


async def d1_query(sql: str, params: list = None, is_select: bool = True) -> dict:
    try:
        body = {"sql": sql, "params": params or [], "token": D1_TOKEN}
        c = await _get_shared_http_client()
        r = await c.post(f"{CF_WORKER_URL}/d1/query", json=body, timeout=15)
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

async def d1_run(sql: str, params: list = None, return_id: bool = False):
    r = await d1_query(sql, params, False)
    ok = r.get("ok")
    last_id = None
    if ok:
        meta = r.get("meta") or {}
        last_id = meta.get("last_row_id")

    # ── Always mirror quiz INSERT/REPLACE to BOTH Supabase accounts, ──
    # ── regardless of D1 success, so web quiz has a backup even when D1 is fine. ──
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
            SB2_URL = "https://xnkuuzstschdovcyomfk.supabase.co"
            SB2_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inhua3V1enN0c2NoZG92Y3lvbWZrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI3NTI3NzUsImV4cCI6MjA5ODMyODc3NX0.rD6p4U1fdqnM2M6t7wA3qsMY1p3KEFD2S1WzSIZehW4"

            async def _mirror_one(url, key):
                if not url or not key:
                    return
                try:
                    headers = {"apikey": key, "Authorization": f"Bearer {key}",
                               "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}
                    async with httpx.AsyncClient(timeout=10) as c:
                        rr = await c.post(f"{url}/rest/v1/quiz_backups",
                                          headers=headers, json=payload)
                    logger.info(f"[D1] Supabase mirror ({url}): {rr.status_code}")
                except Exception as e2:
                    logger.warning(f"[D1] Supabase mirror failed ({url}): {e2}")

            async def _mirror_both():
                await asyncio.gather(
                    _mirror_one(SUPABASE_URL, SUPABASE_KEY),
                    _mirror_one(SB2_URL, SB2_KEY),
                )
            asyncio.create_task(_mirror_both())
    except Exception as e:
        logger.warning(f"[D1] Supabase mirror step failed: {e}")

    if return_id:
        return (bool(ok), last_id)
    return bool(ok)

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
LOG_DIR = os.environ.get("LOG_DIR", "") or "logs"
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
import re as _re_opt

def _strip_option_prefix(text: str) -> str:
    """Strip a leading 'A.'/'A)'/'A:'/'ক.'/'(A)' style label from a poll option,
    so options never show A,B,C,D (or ক,খ,গ,ঘ) prefixes to the user (item 2).
    Requires the separator to be followed by whitespace so legitimate content
    like 'ক-অক্ষর দিয়ে...' (a word that happens to start with ক-) is never
    mistaken for an option label and mangled."""
    if not isinstance(text, str):
        return text
    stripped = _re_opt.sub(
        r"^\s*[\(\[]?\s*[A-Da-dকখগঘ]\s*[\.\)\:।]\s+",
        "", text, count=1
    ).strip()
    return stripped if stripped else text

def _sanitize_poll_options(data: dict) -> dict:
    if isinstance(data.get("options"), list):
        data["options"] = [_strip_option_prefix(o) for o in data["options"]]
    return data

async def tg_post(method: str, data: dict) -> dict:
    if method == "sendPoll":
        data = _sanitize_poll_options(data)
    # ── setWebhook special-case: target URL host must resolve on Telegram's
    #    side. pages.dev/workers.dev subdomains occasionally fail DNS resolve
    #    from Telegram's servers ("Failed to resolve host"). If the primary
    #    domain is used as webhook target and Telegram rejects it with a
    #    resolve error, retry once using the secondary domain instead. ──
    if method == "setWebhook" and _tg_mode == "cf-proxy":
        try:
            client = await _get_shared_http_client()
            r = await client.post(f"{TG_API}/setWebhook", json=data, timeout=60)
            result = r.json()
            if result.get("ok"):
                return result
            desc = (result.get("description") or "")
            if "resolve host" in desc.lower() and CF_WORKER_URL_2 and CF_WORKER_URL in str(data.get("url", "")):
                alt_url = data["url"].replace(CF_WORKER_URL, CF_WORKER_URL_2)
                logger.warning(f"[TG] setWebhook resolve failed on {CF_WORKER_URL}, retrying with {CF_WORKER_URL_2}")
                alt_data = dict(data, url=alt_url)
                r2 = await client.post(f"{TG_API}/setWebhook", json=alt_data, timeout=60)
                result2 = r2.json()
                if result2.get("ok"):
                    logger.info(f"[TG] setWebhook succeeded on fallback domain → {alt_url}")
                    return result2
                logger.warning(f"[TG] setWebhook fallback also failed: {result2.get('description')}")
                return result2
            logger.warning(f"[TG] setWebhook proxy failed: {desc}")
            return result
        except Exception as e:
            logger.warning(f"[TG] setWebhook proxy error: {type(e).__name__}: {e}")
            return {"ok": False, "error": str(e)}
    # ── Primary: CF Worker TG proxy (shared client, short timeout — CF hang/slow
    #    হলে যেন প্রতিটা command 60s আটকে না থেকে দ্রুত direct API-তে fallback করে) ──
    #    The shared client's keep-alive connection can go stale after any idle
    #    period (Space asleep, CF edge recycling idle sockets) — the first
    #    request on a dead connection raises a transport error even though
    #    the service itself is fine. Retry once on transient errors before
    #    falling through to "give up" (which, on HF where direct API is
    #    blocked, previously meant total silent failure on that one stale
    #    connection — exactly "1st command does nothing, 2nd works").
    # getFile নিজেই ছোট/দ্রুত call (Telegram এ file lookup, কোনো bytes না) —
    # তাই এটাকে ছোট timeout দেওয়া হলো (5s vs অন্য method গুলোর 12s), যাতে
    # CF proxy hang/slow হলে /csv এর "ফাইল খোঁজা হচ্ছে..." স্টেপ worst-case
    # 24s+12s=36s এর বদলে দ্রুত fallback এ চলে যায়।
    _proxy_timeout = 5 if method == "getFile" else 12
    for _proxy_attempt in range(2):
        try:
            client = await _get_shared_http_client()
            r = await client.post(f"{TG_API}/{method}", json=data, timeout=_proxy_timeout)
            result = r.json()
            if result.get("ok"):
                return result
            if result.get("error_code") == 429:
                retry_after = result.get("parameters", {}).get("retry_after", 5)
                logger.warning(f"[TG] {method} proxy 429, waiting {retry_after}s")
                await asyncio.sleep(min(retry_after, 30) + 0.5)
            logger.warning(f"[TG] {method} proxy failed: {result.get('description')}")
            break  # got a real response (not a transport error) — don't retry, fall through
        except Exception as e:
            logger.warning(f"[TG] {method} proxy error (attempt {_proxy_attempt+1}/2): {type(e).__name__}: {e}")
            if _proxy_attempt == 0:
                continue  # retry once — likely a stale reused connection
    # ── Fallback: Secondary CF Worker domain (primary just failed twice —
    #    could be that specific edge/domain having issues while the 2nd
    #    domain, already proven reachable for setWebhook, still works).
    #    Only meaningful in cf-proxy mode; skip if no 2nd domain configured. ──
    if _tg_mode == "cf-proxy" and CF_WORKER_URL_2:
        try:
            alt_api = f"{CF_WORKER_URL_2}/tg-proxy"
            client = await _get_shared_http_client()
            r = await client.post(f"{alt_api}/{method}", json=data, timeout=_proxy_timeout)
            result = r.json()
            if result.get("ok"):
                logger.info(f"[TG] {method} recovered via secondary CF Worker ({CF_WORKER_URL_2})")
                return result
            logger.warning(f"[TG] {method} secondary CF proxy also failed: {result.get('description')}")
        except Exception as e:
            logger.warning(f"[TG] {method} secondary CF proxy error: {type(e).__name__}: {e}")
    # ── Fallback: Direct Telegram API (skipped when running where TG is
    #    network-blocked, e.g. HF Space — trying it there just wastes time
    #    on a guaranteed failure before eventually giving up) ──
    if _tg_mode == "cf-proxy":
        logger.error(f"[TG] {method} CF proxy failed and direct API is blocked on this platform — giving up")
        return {"ok": False, "error": "cf_proxy_failed_direct_blocked"}
    for attempt in range(2):
        try:
            client = await _get_shared_http_client()
            r = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=data, timeout=60)
            result = r.json()
            if not result.get("ok"):
                if result.get("error_code") == 429:
                    retry_after = result.get("parameters", {}).get("retry_after", 5)
                    logger.warning(f"[TG] {method} direct 429, waiting {retry_after}s")
                    await asyncio.sleep(min(retry_after, 30) + 0.5)
                logger.warning(f"[TG] {method} direct failed: {result.get('description')}")
            return result
        except Exception as e:
            logger.error(f"[TG] {method} direct error (attempt {attempt+1}/2): {type(e).__name__}: {e}")
            if attempt == 0:
                await asyncio.sleep(1.5)
                continue
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

async def edit_msg_caption(chat_id, message_id: int, caption: str, parse_mode: str = "HTML") -> dict:
    return await tg_post("editMessageCaption", {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": caption,
        "parse_mode": parse_mode
    })

async def send_photo(chat_id, photo_bytes: bytes, caption: str = "",
                     reply_markup=None, reply_to_message_id: int = None,
                     message_thread_id: int = None) -> dict:
    # ── Primary: CF Worker (b64 proxy, shared client) ──
    try:
        b64 = base64.b64encode(photo_bytes).decode()
        data = {"chat_id": str(chat_id), "caption": caption, "photo_b64": b64}
        if reply_markup: data["reply_markup"] = reply_markup
        if reply_to_message_id: data["reply_to_message_id"] = reply_to_message_id
        if message_thread_id: data["message_thread_id"] = message_thread_id
        client = await _get_shared_http_client()
        r = await client.post(f"{CF_WORKER_URL}/tg-sendphoto", json=data, timeout=60)
        result = r.json()
        if result.get("ok"): return result
    except Exception as e:
        logger.warning(f"[TG] sendPhoto CF failed: {e}")
    # ── Fallback: Direct TG API multipart (CF down হলে, shared client) ──
    try:
        fields = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
        if reply_to_message_id: fields["reply_to_message_id"] = str(reply_to_message_id)
        if message_thread_id: fields["message_thread_id"] = str(message_thread_id)
        if reply_markup:
            import json as _j
            fields["reply_markup"] = _j.dumps(reply_markup)
        files = {"photo": ("photo.jpg", photo_bytes, "image/jpeg")}
        client = await _get_shared_http_client()
        r = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data=fields, files=files, timeout=120)
        return r.json()
    except Exception as e:
        logger.error(f"[TG] sendPhoto direct failed: {e}")
        return {"ok": False, "error": str(e)}

async def send_photo_by_id(chat_id, file_id: str, caption: str = "",
                           parse_mode: str = "HTML", reply_to_message_id: int = None) -> dict:
    data = {
        "chat_id": chat_id,
        "photo": file_id,
        "caption": caption,
        "parse_mode": parse_mode
    }
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    return await tg_post("sendPhoto", data)

async def send_media_group(chat_id, photos: list, reply_to_message_id: int = None) -> dict:
    """Send up to 10 photos as a single Telegram album (media group).
    photos: list of (filename, bytes) tuples, in the order they should appear.
    Uses direct multipart upload (attach://) since CF Worker's JSON proxy
    doesn't support multi-file album uploads."""
    import json as _j
    media = []
    files = {}
    for i, (fname, fbytes) in enumerate(photos):
        key = f"photo{i}"
        media.append({"type": "photo", "media": f"attach://{key}"})
        files[key] = (fname, fbytes, "image/jpeg")
    fields = {"chat_id": str(chat_id), "media": _j.dumps(media)}
    if reply_to_message_id:
        fields["reply_to_message_id"] = str(reply_to_message_id)
    try:
        c = await _get_shared_http_client()
        r = await c.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMediaGroup",
            data=fields, files=files, timeout=180)
        return r.json()
    except Exception as e:
        logger.error(f"[sendMediaGroup] failed: {e}")
        return {"ok": False, "error": str(e)}

async def send_document(chat_id, file_bytes: bytes, filename: str,
                        caption: str = "", mime_type="application/octet-stream",
                        reply_to_message_id: int = None, parse_mode: str = "HTML",
                        message_thread_id: int = None) -> dict:
    # ── Primary: CF Worker (b64 proxy, shared client) ──
    try:
        data = {
            "chat_id": str(chat_id), "caption": caption, "parse_mode": parse_mode,
            "filename": filename, "mime_type": mime_type,
            "doc_b64": base64.b64encode(file_bytes).decode()
        }
        if reply_to_message_id: data["reply_to_message_id"] = reply_to_message_id
        if message_thread_id: data["message_thread_id"] = message_thread_id
        c = await _get_shared_http_client()
        r = await c.post(f"{CF_WORKER_URL}/tg-senddoc", json=data, timeout=60)
        result = r.json()
        if result.get("ok"): return result
    except Exception as e:
        logger.warning(f"[sendDoc] CF failed: {e}")
    # ── Fallback: Direct TG API multipart (shared client) ──
    try:
        fields = {"chat_id": str(chat_id), "caption": caption, "parse_mode": parse_mode}
        if reply_to_message_id: fields["reply_to_message_id"] = str(reply_to_message_id)
        if message_thread_id: fields["message_thread_id"] = str(message_thread_id)
        files = {"document": (filename, file_bytes, mime_type)}
        c = await _get_shared_http_client()
        r = await c.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
            data=fields, files=files, timeout=120)
        return r.json()
    except Exception as e:
        logger.error(f"[sendDoc] direct failed: {e}")
        return {"ok": False, "error": str(e)}

def extract_image_url(text: str):
    """<img src="URL"> ট্যাগ থেকে image URL বের করে, বাকি টেক্সট ক্লিন করে রিটার্ন করে।"""
    if not text:
        return None, text
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', text)
    if not match:
        return None, text
    url = match.group(1)
    clean_text = re.sub(r'<img[^>]+>', '', text).strip()
    return url, clean_text


async def send_poll(chat_id, question: str, options: list, correct_idx: int,
                    explanation: str = "", reply_to_message_id: int = None,
                    message_thread_id: int = None, is_anonymous: bool = True,
                    open_period: int = None, poll_type: str = "quiz") -> dict:
    # প্রতিটা অংশ (question/option/explanation) থেকে <img> ট্যাগ থাকলে আলাদা করা হচ্ছে —
    # Bot API 10.0 (May 2026)-এ InputPollMedia/InputPollOptionMedia/explanation_media
    # যোগ হয়েছে, যেটা দিয়ে poll question/option/explanation-এ ছবি embed করা যায়।
    q_img_url, q_clean = extract_image_url(question)
    exp_img_url, exp_clean = extract_image_url(explanation)

    options_list = []
    api_options = []
    has_opt_image = False
    for opt in options:
        img_url, clean_opt = extract_image_url(opt)
        clean_opt = (clean_opt or opt)[:100]
        options_list.append(clean_opt)
        if img_url:
            has_opt_image = True
            api_options.append({"text": clean_opt, "media": {"type": "photo", "media": img_url}})
        else:
            api_options.append({"text": clean_opt})

    base_data = {
        "chat_id": chat_id,
        "type": poll_type,
        "correct_option_id": correct_idx,
        "is_anonymous": is_anonymous,
    }
    if open_period:
        base_data["open_period"] = open_period
    if reply_to_message_id:
        base_data["reply_to_message_id"] = reply_to_message_id
    if message_thread_id:
        base_data["message_thread_id"] = message_thread_id

    has_any_image = bool(q_img_url or has_opt_image or exp_img_url)

    if has_any_image:
        media_data = dict(base_data)
        media_data["question"] = (q_clean or question)[:300]
        media_data["options"] = api_options
        media_data["explanation"] = (exp_clean or explanation)[:200]
        if q_img_url:
            media_data["media"] = {"type": "photo", "media": q_img_url}
        if exp_img_url:
            media_data["explanation_media"] = {"type": "photo", "media": exp_img_url}
        result = await tg_post("sendPoll", media_data)
        if result.get("ok"):
            return result
        logger.warning(f"[send_poll] Media poll failed, falling back to text-only: {result.get('description')}")

    # Fallback: প্লেইন টেক্সট poll (image ছাড়া) — media schema fail করলে বা কোনো image না থাকলে
    plain_data = dict(base_data)
    plain_data["question"] = (q_clean or question)[:300]
    plain_data["options"] = options_list
    plain_data["explanation"] = (exp_clean or explanation)[:200]
    return await tg_post("sendPoll", plain_data)

_OWNER_JOB_MSG = {}  # job_key -> {"msg_id": int, "lines": [str]}

async def notify_owner(text: str, job_key: str = None):
    """job_key: pass the same key for every alert belonging to one logical
    job (e.g. one /csv run) — instead of each call sending a brand new
    message, they all edit a single rolling message, appending a new line
    per step. Without job_key, behaves exactly as before (one-off message)."""
    if not OWNER_ID:
        return
    if not job_key:
        await send_msg(OWNER_ID, f"🔔 <b>ATLAS BOT Alert</b>\n\n{text}")
        return

    state = _OWNER_JOB_MSG.get(job_key)
    if state is None:
        r = await send_msg(OWNER_ID, f"🔔 <b>ATLAS BOT Alert</b>\n\n{text}")
        msg_id = r.get("result", {}).get("message_id") if r.get("ok") else None
        _OWNER_JOB_MSG[job_key] = {"msg_id": msg_id, "lines": [text]}
        return

    state["lines"].append(text)
    body = "\n\n".join(state["lines"])[-3800:]  # stay under Telegram's 4096-char cap
    if state["msg_id"]:
        r = await edit_msg(OWNER_ID, state["msg_id"], f"🔔 <b>ATLAS BOT Alert</b>\n\n{body}")
        if not r.get("ok"):
            # message may have been deleted/too old to edit — fall back to a fresh one
            r2 = await send_msg(OWNER_ID, f"🔔 <b>ATLAS BOT Alert</b>\n\n{body}")
            state["msg_id"] = r2.get("result", {}).get("message_id") if r2.get("ok") else None
    else:
        r = await send_msg(OWNER_ID, f"🔔 <b>ATLAS BOT Alert</b>\n\n{body}")
        state["msg_id"] = r.get("result", {}).get("message_id") if r.get("ok") else None

def clear_owner_job(job_key: str):
    """Call once a job is fully done (success or fail) so the next run
    starts a fresh rolling message instead of appending to a stale one."""
    _OWNER_JOB_MSG.pop(job_key, None)

async def notify_owner_edit(text: str, msg_id_box: dict):
    """Single-message progress notifier — edits the same owner message
    throughout a job instead of sending a new message per stage, so the
    owner's chat doesn't get flooded with one line per step."""
    if not OWNER_ID:
        return
    full = f"🔔 <b>ATLAS BOT Alert</b>\n\n{text}"
    mid = msg_id_box.get("id")
    if mid:
        r = await edit_msg(OWNER_ID, mid, full)
        if r and r.get("ok"):
            return
    r = await send_msg(OWNER_ID, full)
    new_id = r.get("result", {}).get("message_id")
    if new_id:
        msg_id_box["id"] = new_id

# ============================================================
# PYROGRAM CLIENT (large file download, >20MB, Bot API getFile bypass)
# ============================================================
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
_pyro_client = None

_shared_http_client = None

async def _get_shared_http_client():
    global _shared_http_client
    if _shared_http_client is None:
        # keepalive_expiry বাড়িয়ে দিলাম (httpx default মাত্র 5s) — কম গ্যাপে
        # commands এলে পুরনো connection বেশি সময় বেঁচে থাকবে, তাই "প্রথমবার
        # stale connection-এ fail করে retry লাগে, দ্বিতীয়বার fast" — এই
        # pattern-টাই কম ঘটবে (retry logic এখনও fallback হিসেবে থাকছে)।
        limits = httpx.Limits(max_keepalive_connections=20, keepalive_expiry=60.0)
        _shared_http_client = httpx.AsyncClient(timeout=300, limits=limits)
    return _shared_http_client

async def _get_pyro_client():
    global _pyro_client
    if _pyro_client is None and TELEGRAM_API_ID and TELEGRAM_API_HASH:
        from pyrogram import Client
        client = Client(
            "atlas_pyrogram", api_id=int(TELEGRAM_API_ID),
            api_hash=TELEGRAM_API_HASH, bot_token=BOT_TOKEN, no_updates=True,
            in_memory=True, max_concurrent_transmissions=8,
        )
        try:
            await client.start()
        except Exception as e:
            # Don't cache a client whose start() failed (e.g. FLOOD_WAIT on
            # auth.ImportBotAuthorization) — leaving _pyro_client set to a
            # never-started client here permanently breaks every future
            # private-invite-link resolve/large-file download until restart.
            # Keep it None so the NEXT call retries start() fresh.
            logger.warning(f"[pyrogram] start() failed, will retry next call: {e}")
            return None
        _pyro_client = client
    return _pyro_client

async def download_large_file_pyrogram(chat_id: int, message_id: int, progress_cb=None) -> Optional[bytes]:
    try:
        client = await _get_pyro_client()
        if not client:
            logger.error("[pyrogram] TELEGRAM_API_ID/HASH not set, cannot download large file")
            return None
        msg = await client.get_messages(chat_id, message_id)
        if not msg or not (msg.document or msg.video or msg.audio):
            return None

        _progress_fn = None
        if progress_cb:
            async def _progress_fn(current, total):
                try:
                    res = progress_cb(current, total)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass

        file_bytes = await client.download_media(msg, in_memory=True, progress=_progress_fn)
        if file_bytes is None:
            return None
        return file_bytes.getvalue() if hasattr(file_bytes, "getvalue") else file_bytes
    except Exception as e:
        logger.error(f"[pyrogram] download error: {e}")
        return None

async def resolve_private_invite_link(invite_link: str) -> dict:
    """
    Private invite link (t.me/+xxx বা t.me/joinchat/xxx) থেকে chat ID resolve করে।
    TELEGRAM_API_ID/HASH configured থাকলে Pyrogram দিয়ে join try করে, না থাকলে error।
    Returns: {"ok": True, "id":..., "title":..., "type":...} অথবা {"ok": False, "error": "..."}
    """
    client = await _get_pyro_client()
    if not client:
        return {"ok": False, "error": "TELEGRAM_API_ID/TELEGRAM_API_HASH সেট করা নাই, অথবা Telegram সাময়িকভাবে rate-limit করেছে (কিছুক্ষণ পর আবার চেষ্টা করো)।"}
    try:
        chat = await client.join_chat(invite_link)
        return {
            "ok": True,
            "id": chat.id,
            "title": getattr(chat, "title", "") or getattr(chat, "first_name", ""),
            "type": str(getattr(chat, "type", "")).split(".")[-1].lower(),
            "username": getattr(chat, "username", None),
        }
    except Exception as e:
        err = str(e)
        # ইতিমধ্যে join করা থাকলে join_chat error দেয়, কিন্তু chat resolve করা যায়
        if "USER_ALREADY_PARTICIPANT" in err or "already" in err.lower():
            try:
                chat = await client.get_chat(invite_link)
                return {
                    "ok": True,
                    "id": chat.id,
                    "title": getattr(chat, "title", "") or getattr(chat, "first_name", ""),
                    "type": str(getattr(chat, "type", "")).split(".")[-1].lower(),
                    "username": getattr(chat, "username", None),
                }
            except Exception as e2:
                return {"ok": False, "error": str(e2)}
        return {"ok": False, "error": err}

# ============================================================
# DOWNLOAD FILE VIA CF PROXY
# ============================================================
async def download_tg_file(file_id: str, progress_cb=None,
                           chat_id: int = None, message_id: int = None) -> bytes:
    # Pyrogram is now the DEFAULT download path (not just a >20MB fallback) --
    # it has no Bot API 20MB ceiling and is a single consistent code path for
    # every file size. Falls back to Bot API getFile if pyrogram isn't
    # configured (no TELEGRAM_API_ID/HASH) or the call itself fails.
    if chat_id is not None and message_id is not None:
        big = await download_large_file_pyrogram(chat_id, message_id, progress_cb=progress_cb)
        if big is not None:
            return big
        logger.warning("[download_tg_file] pyrogram unavailable/failed, falling back to Bot API getFile")

    if progress_cb:
        try:
            res = progress_cb(0, 0)  # signal: getFile lookup in progress, size unknown yet
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            pass

    file_res = await tg_post("getFile", {"file_id": file_id})
    if not file_res.get("ok"):
        desc = file_res.get("description")
        if not desc:
            raise Exception(
                "getFile failed: file likely exceeds Telegram Bot API's 20MB download "
                "limit (large multi-page PDFs often do), and pyrogram fallback also "
                "failed — check TELEGRAM_API_ID/TELEGRAM_API_HASH env vars."
            )
        raise Exception(f"getFile failed: {desc}")
    file_path = file_res["result"]["file_path"]
    total_size = file_res["result"].get("file_size", 0)

    async def _stream_download(url: str) -> bytes:
        # In-memory buffer — ছোট/মাঝারি ফাইলে (bot download সাধারণত <50MB)
        # disk write/read এর extra I/O latency বাদ দিলে ভালো speed পাওয়া যায়।
        # 10s explicit timeout — আগে shared client-এর 300s default inherit
        # হতো, তাই CF Worker/Telegram ওপাশে hang করলে "0%-এ আটকে থাকা" screen
        # কয়েক মিনিট পর্যন্ত silently চলতে পারতো কোনো error/fallback ছাড়াই।
        # CSV ছোট ফাইল, 10s এর বেশি লাগলে সেটা আসলেই একটা failure — দ্রুত
        # fail করে caller-কে জানানো ভালো, চুপচাপ hang হওয়ার চেয়ে।
        downloaded = 0
        buf = io.BytesIO()
        client = await _get_shared_http_client()
        async with client.stream("GET", url, timeout=10) as r:
            if r.status_code != 200:
                raise Exception(f"HTTP {r.status_code}")
            async for chunk in r.aiter_bytes(chunk_size=1048576):  # 1MB chunks
                buf.write(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    try:
                        res = progress_cb(downloaded, total_size)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception:
                        pass
        return buf.getvalue()

    # HF-এ (_tg_mode == "cf-proxy") direct Telegram network-level blocked —
    # আগে এখানে সবসময় প্রথমে direct try করা হতো, shared client-এর 300s
    # default timeout-এর কারণে সেই attempt fail/hang হতে অনেক সময় লাগতে
    # পারতো প্রতিটা /csv-তে, তারপর CF proxy fallback চলতো। এখন platform
    # অনুযায়ী সরাসরি সঠিক path-এ যাওয়া হচ্ছে — HF হলে CF proxy দিয়েই শুরু,
    # Render/direct মোডে direct API দিয়েই শুরু (যেখানে সেটা আসলে কাজ করে)।
    if _tg_mode == "cf-proxy":
        return await _stream_download(f"{CF_WORKER_URL}/tg-file?path={file_path}")
    try:
        return await _stream_download(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}")
    except Exception as e:
        logger.warning(f"[Download] Direct Telegram failed, trying CF proxy: {e}")
    return await _stream_download(f"{CF_WORKER_URL}/tg-file?path={file_path}")

# ============================================================
# SUPABASE HELPERS (shared)
# ============================================================
_D1_TABLES_ENSURED = set()

async def _ensure_d1_table(name: str, create_sql: str):
    if name in _D1_TABLES_ENSURED:
        return
    try:
        await d1_run(create_sql)
        _D1_TABLES_ENSURED.add(name)
    except Exception as e:
        logger.warning(f"[D1] ensure {name} table warn: {e}")

async def db_get_settings() -> dict:
    try:
        r = await sb_exec(lambda: sb.table("quiz_settings").select("tag,exp_footer,watermark").eq("id", 1).execute())
        if r.data:
            return r.data[0]
    except Exception as e:
        # watermark column ekhono Supabase e add kora hoy nai (migration pending) —
        # purono columns diye retry kore crash bachai, watermark khali thakbe
        if "watermark" in str(e):
            try:
                r = await sb_exec(lambda: sb.table("quiz_settings").select("tag,exp_footer").eq("id", 1).execute())
                if r.data:
                    row = r.data[0]
                    row["watermark"] = ""
                    return row
            except Exception as e2:
                logger.error(f"[DB] get_settings retry error: {e2}")
        else:
            logger.error(f"[DB] get_settings error: {e}")
    try:
        await _ensure_d1_table("quiz_settings",
            "CREATE TABLE IF NOT EXISTS quiz_settings (id INTEGER PRIMARY KEY, tag TEXT, exp_footer TEXT, watermark TEXT)")
        rows = await d1_select("SELECT tag, exp_footer, watermark FROM quiz_settings WHERE id=1")
        if rows:
            return rows[0]
    except Exception as e:
        logger.warning(f"[D1] get_settings fallback warn: {e}")
    return {"tag": "", "exp_footer": "", "watermark": ""}

async def db_save_settings_field(field: str, value: str):
    try:
        await _ensure_d1_table("quiz_settings",
            "CREATE TABLE IF NOT EXISTS quiz_settings (id INTEGER PRIMARY KEY, tag TEXT, exp_footer TEXT, watermark TEXT)")
        await d1_run(
            f"INSERT INTO quiz_settings (id, {field}) VALUES (1, ?1) "
            f"ON CONFLICT(id) DO UPDATE SET {field}=excluded.{field}",
            [value]
        )
    except Exception as e:
        logger.warning(f"[D1] save_settings mirror warn: {e}")

async def db_save_settings(settings: dict):
    """dict-e thaka shob field Supabase (primary) + D1 (mirror) e save kore.
    /wm command er moto jekhane pura settings dict update hoy shekhane use hoy."""
    try:
        await sb_exec(lambda: sb.table("quiz_settings").upsert({"id": 1, **settings}).execute())
    except Exception as e:
        logger.error(f"[DB] save_settings error: {e}")
    for field, value in settings.items():
        await db_save_settings_field(field, value)

_admin_check_cache = {}  # uid -> (is_admin: bool, checked_at: float)
_ADMIN_CACHE_TTL = 300  # 5 min

async def db_is_owner_or_admin(uid: int) -> bool:
    if uid == OWNER_ID:
        return True
    now = time.time()
    cached = _admin_check_cache.get(uid)
    if cached and (now - cached[1]) < _ADMIN_CACHE_TTL:
        return cached[0]
    try:
        r = await sb_exec(lambda: sb.table("admins").select("user_id").eq("user_id", uid).execute())
        result = len(r.data) > 0
        _admin_check_cache[uid] = (result, now)
        return result
    except:
        return False

async def db_track_user(uid: int, uname: str):
    async def _sb_write():
        try:
            await sb_exec(lambda: sb.table("pdf_users").upsert({
                "user_id": uid, "user_name": uname, "last_seen": int(time.time())
            }).execute())
        except Exception as e:
            logger.error(f"[DB] track_user error: {e}")

    async def _d1_write():
        try:
            await _ensure_d1_table("pdf_users",
                "CREATE TABLE IF NOT EXISTS pdf_users (user_id INTEGER PRIMARY KEY, user_name TEXT, last_seen INTEGER)")
            await d1_run(
                "INSERT INTO pdf_users (user_id,user_name,last_seen) VALUES (?1,?2,?3) "
                "ON CONFLICT(user_id) DO UPDATE SET user_name=excluded.user_name, last_seen=excluded.last_seen",
                [uid, uname, int(time.time())]
            )
        except Exception as e:
            logger.warning(f"[D1] track_user mirror warn: {e}")

    # Supabase + D1 write একসাথে (sequential হলে প্রতি মেসেজে ২টা network round-trip
    # যোগ হতো — parallel করায় সময় প্রায় অর্ধেক)
    await asyncio.gather(_sb_write(), _d1_write())

async def db_save_session(session_id: str, data: dict):
    try:
        await sb_exec(lambda: sb.table("pdf_sessions").upsert({"id": session_id, **data}).execute())
    except Exception as e:
        logger.error(f"[DB] save_session error: {e}")

async def db_save_mcq_cache(cache_id: str, session_id: str, page: int,
                             topic: str, mcqs: list, poll_links: list = None,
                             image_file_id: str = None, image_msg_id: int = None,
                             channel_id: str = None, is_new_gen: bool = False,
                             end_msg_id: int = None):
    try:
        await sb_exec(lambda: sb.table("pdf_mcq_cache").upsert({
            "id": cache_id, "session_id": session_id, "page_number": page,
            "topic": topic, "mcq_data": mcqs, "poll_links": poll_links or [],
            "image_file_id": image_file_id, "image_msg_id": image_msg_id,
            "channel_id": channel_id or "", "is_new_gen": is_new_gen,
            "end_msg_id": end_msg_id, "new_gen_count": 0
        }).execute())
    except Exception as e:
        logger.error(f"[DB] save_mcq_cache error: {e}")

    async def _d1_write():
        # DURABILITY: mirror into D1 `quizzes` table too (same table/schema the
        # qz_ web-quiz path already uses) so the Website Exam link survives even
        # if Supabase (primary store above) is ever unreachable/deleted — get_exam_data
        # already knows how to read this table as a fallback source.
        # Non-critical backup copy — fire-and-forget so commands like /csv
        # respond instantly instead of waiting on a 2nd sequential DB round-trip.
        try:
            import json as _json
            await d1_run(
                "INSERT OR REPLACE INTO quizzes (id,name,description,timer,shuffle,csv_data,tag,exp_footer,created_by) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                [cache_id, topic or "ATLAS MCQ", "", 30, 0, _json.dumps(mcqs), "", "", 0]
            )
        except Exception as e:
            logger.warning(f"[DB] D1 mirror for pdf_mcq_cache failed (non-fatal): {e}")

    asyncio.create_task(_d1_write())

async def db_update_cache(cache_id: str, fields: dict):
    try:
        await sb_exec(lambda: sb.table("pdf_mcq_cache").update(fields).eq("id", cache_id).execute())
    except Exception as e:
        logger.error(f"[DB] update_cache error: {e}")

async def db_get_mcq_cache(cache_id: str) -> dict:
    try:
        r = await sb_exec(lambda: sb.table("pdf_mcq_cache").select("*").eq("id", cache_id).execute())
        if r.data:
            return r.data[0]
    except Exception as e:
        logger.error(f"[DB] get_mcq_cache error: {e}")
    # DURABILITY FALLBACK: Supabase (primary) failed/empty — try the D1 mirror
    # (written by db_save_mcq_cache above) so the exam link still works.
    try:
        rows = await d1_select("SELECT * FROM quizzes WHERE id=?1", [cache_id])
        if rows:
            row = rows[0]
            import json as _json
            mcqs = _json.loads(row.get("csv_data", "[]"))
            return {
                "id": cache_id, "session_id": cache_id, "page_number": 1,
                "topic": row.get("name", "ATLAS MCQ"), "mcq_data": mcqs,
                "poll_links": [], "image_file_id": None, "image_msg_id": None,
                "channel_id": "", "is_new_gen": False, "end_msg_id": None,
            }
    except Exception as e:
        logger.warning(f"[DB] D1 fallback for get_mcq_cache failed: {e}")
    return None

async def db_get_new_gen_count(cache_id: str, user_id: int) -> int:
    try:
        r = await sb_exec(lambda: sb.table("new_gen_count").select("count")
            .eq("cache_id", cache_id).eq("user_id", user_id).execute())
        if r.data:
            return r.data[0]["count"]
    except:
        pass
    return 0

async def db_increment_gen_count(cache_id: str, user_id: int) -> int:
    try:
        count = await db_get_new_gen_count(cache_id, user_id) + 1
        await sb_exec(lambda: sb.table("new_gen_count").upsert({
            "cache_id": cache_id, "user_id": user_id,
            "count": count, "updated_at": int(time.time())
        }).execute())
        return count
    except Exception as e:
        logger.error(f"[DB] increment_gen_count error: {e}")
        return 0

async def db_save_leaderboard(cache_id: str, user_id: int, user_name: str,
                               topic: str, page: int, correct: int,
                               total: int, final_score: float):
    try:
        r = await sb_exec(lambda: sb.table("web_exam_leaderboard").select("final_score")
            .eq("cache_id", cache_id).eq("user_id", user_id).execute())
        if r.data:
            if final_score > r.data[0]["final_score"]:
                await sb_exec(lambda: sb.table("web_exam_leaderboard").update({
                    "user_name": user_name, "correct": correct,
                    "total": total, "final_score": final_score,
                    "updated_at": int(time.time())
                }).eq("cache_id", cache_id).eq("user_id", user_id).execute())
        else:
            await sb_exec(lambda: sb.table("web_exam_leaderboard").insert({
                "cache_id": cache_id, "user_id": user_id, "user_name": user_name,
                "topic": topic, "page_number": page, "correct": correct,
                "total": total, "final_score": final_score
            }).execute())
    except Exception as e:
        logger.error(f"[DB] save_leaderboard error: {e}")
    try:
        await _ensure_d1_table("web_exam_leaderboard",
            "CREATE TABLE IF NOT EXISTS web_exam_leaderboard (cache_id TEXT, user_id INTEGER, user_name TEXT, "
            "topic TEXT, page_number INTEGER, correct INTEGER, total INTEGER, final_score REAL, updated_at INTEGER, "
            "PRIMARY KEY (cache_id, user_id))")
        await d1_run(
            "INSERT INTO web_exam_leaderboard (cache_id,user_id,user_name,topic,page_number,correct,total,final_score,updated_at) "
            "VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9) "
            "ON CONFLICT(cache_id,user_id) DO UPDATE SET user_name=excluded.user_name, correct=excluded.correct, "
            "total=excluded.total, final_score=excluded.final_score, updated_at=excluded.updated_at "
            "WHERE excluded.final_score > web_exam_leaderboard.final_score",
            [cache_id, user_id, user_name, topic, page, correct, total, final_score, int(time.time())]
        )
    except Exception as e:
        logger.warning(f"[D1] save_leaderboard mirror warn: {e}")

_D1_CHANNELS_TABLE_ENSURED = False

# ============================================================
# CSV/CSVS POLL-JOB PROGRESS — CRASH/RESTART RESUME
# ============================================================
# HF Space restart/crash হলে আগে সব চলমান /csv poll-sending job memory থেকে
# হারিয়ে যেত (asyncio.Queue শুধু RAM-এ থাকে) — user কে পুরো batch আবার
# শুরু থেকে পাঠাতে হতো (duplicate polls সহ)। এখন প্রতিটা poll পাঠানোর পরে
# progress D1-এ save হয়, আর bot startup-এ অসম্পূর্ণ job থাকলে সেখান থেকে
# resume করে — কোনো poll মিস বা duplicate ছাড়াই।
async def _ensure_csv_job_table():
    await _ensure_d1_table("csv_poll_jobs",
        "CREATE TABLE IF NOT EXISTS csv_poll_jobs ("
        "job_id TEXT PRIMARY KEY, cache_id TEXT, channel_id TEXT, chat_id INTEGER, uid INTEGER, "
        "mode TEXT, batch_size INTEGER, topic TEXT, csv_fname TEXT, thread_id INTEGER, "
        "loading_id INTEGER, sent_index INTEGER, total INTEGER, first_poll_link TEXT, "
        "status TEXT, updated_at INTEGER)")

async def db_save_csv_job(job_id: str, **fields):
    """Job শুরু হওয়ার সময় বা progress update হওয়ার সময় কল হয়। fields-এ যা
    দেওয়া হবে শুধু সেগুলোই upsert হবে (partial update, non-fatal on error —
    progress-save fail হলেও poll পাঠানো থেমে থাকবে না)।"""
    try:
        await _ensure_csv_job_table()
        cols = ["job_id"] + list(fields.keys()) + ["updated_at"]
        placeholders = ",".join(f"?{i+1}" for i in range(len(cols)))
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "job_id")
        vals = [job_id] + list(fields.values()) + [int(time.time())]
        await d1_run(
            f"INSERT INTO csv_poll_jobs ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(job_id) DO UPDATE SET {updates}",
            vals
        )
    except Exception as e:
        logger.warning(f"[D1] save_csv_job warn (non-fatal, job continues): {e}")

async def db_update_csv_job_progress(job_id: str, sent_index: int, first_poll_link: str = None):
    try:
        await _ensure_csv_job_table()
        if first_poll_link:
            await d1_run(
                "UPDATE csv_poll_jobs SET sent_index=?1, first_poll_link=?2, updated_at=?3 WHERE job_id=?4",
                [sent_index, first_poll_link, int(time.time()), job_id]
            )
        else:
            await d1_run(
                "UPDATE csv_poll_jobs SET sent_index=?1, updated_at=?2 WHERE job_id=?3",
                [sent_index, int(time.time()), job_id]
            )
    except Exception as e:
        logger.warning(f"[D1] update_csv_job_progress warn (non-fatal): {e}")

async def db_finish_csv_job(job_id: str):
    try:
        await _ensure_csv_job_table()
        await d1_run("UPDATE csv_poll_jobs SET status='done', updated_at=?1 WHERE job_id=?2",
                     [int(time.time()), job_id])
    except Exception as e:
        logger.warning(f"[D1] finish_csv_job warn (non-fatal): {e}")

async def db_get_incomplete_csv_jobs() -> list:
    """Startup-এ কল হয় — status='running' রেখে যাওয়া job মানেই আগের
    process restart/crash-এ মাঝপথে থেমে গেছে। 24 ঘণ্টার বেশি পুরনো job
    resume করা হয় না (session/cache ততক্ষণে expired হয়ে যাওয়ার কথা)।"""
    try:
        await _ensure_csv_job_table()
        cutoff = int(time.time()) - 86400
        rows = await d1_select(
            "SELECT * FROM csv_poll_jobs WHERE status='running' AND updated_at > ?1",
            [cutoff]
        )
        return rows or []
    except Exception as e:
        logger.warning(f"[D1] get_incomplete_csv_jobs warn: {e}")
        return []

async def _ensure_d1_channels_table():
    global _D1_CHANNELS_TABLE_ENSURED
    if _D1_CHANNELS_TABLE_ENSURED:
        return
    try:
        await d1_run(
            "CREATE TABLE IF NOT EXISTS channels ("
            "channel_id TEXT PRIMARY KEY, channel_name TEXT)"
        )
        _D1_CHANNELS_TABLE_ENSURED = True
    except Exception as e:
        logger.warning(f"[D1] ensure channels table warn: {e}")

async def db_save_channel(channel_id: str, channel_name: str) -> bool:
    """Save/update a channel in BOTH Supabase (primary) and D1 (mirror/durability)."""
    ok = True
    try:
        await sb_exec(lambda: sb.table("channels").upsert({"channel_id": channel_id, "channel_name": channel_name}).execute())
    except Exception as e:
        logger.error(f"[DB] save_channel Supabase error: {e}")
        ok = False
    try:
        await _ensure_d1_channels_table()
        await d1_run(
            "INSERT INTO channels (channel_id, channel_name) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET channel_name=excluded.channel_name",
            [channel_id, channel_name]
        )
    except Exception as e:
        logger.warning(f"[D1] save_channel mirror warn: {e}")
    return ok

async def db_get_channels() -> list:
    try:
        r = await sb_exec(lambda: sb.table("channels").select("*").execute())
        if r.data:
            return r.data
    except Exception as e:
        logger.warning(f"[DB] get_channels Supabase warn: {e}")
    # ── Supabase empty/down → D1 fallback ──
    try:
        await _ensure_d1_channels_table()
        rows = await d1_select("SELECT channel_id, channel_name FROM channels")
        return rows or []
    except Exception as e:
        logger.warning(f"[D1] get_channels fallback warn: {e}")
        return []

async def db_delete_channel(channel_id: str) -> bool:
    ok = True
    try:
        await sb_exec(lambda: sb.table("channels").delete().eq("channel_id", channel_id).execute())
    except Exception as e:
        logger.error(f"[DB] delete_channel Supabase error: {e}")
        ok = False
    try:
        await _ensure_d1_channels_table()
        await d1_run("DELETE FROM channels WHERE channel_id = ?", [channel_id])
    except Exception as e:
        logger.warning(f"[D1] delete_channel mirror warn: {e}")
    return ok

async def db_rename_channel(channel_id: str, new_name: str) -> bool:
    ok = True
    try:
        await sb_exec(lambda: sb.table("channels").update({"channel_name": new_name}).eq("channel_id", channel_id).execute())
    except Exception as e:
        logger.error(f"[DB] rename_channel Supabase error: {e}")
        ok = False
    try:
        await _ensure_d1_channels_table()
        await d1_run("UPDATE channels SET channel_name = ? WHERE channel_id = ?", [new_name, channel_id])
    except Exception as e:
        logger.warning(f"[D1] rename_channel mirror warn: {e}")
    return ok

# ============================================================
# QUIZ STATE (last-quiz resume, shared by image/pdf quiz solve)
# ============================================================
async def db_save_last_quiz(uid: int, st: dict):
    try:
        await sb_exec(lambda: sb.table("quiz_last_state").upsert({
            "user_id": uid, "cache_id": st["cache_id"],
            "topic": st.get("topic", ""), "page_number": st.get("page", 1),
            "mcqs": st.get("mcqs", []), "wrong_idx": st.get("wrong_idx", []),
            "skip_idx": st.get("skip_idx", []), "src_indices": st.get("src_indices"),
            "channel_id": st.get("channel_id", ""), "back_msg_id": st.get("back_msg_id"),
            "is_new_gen": bool(st.get("is_new_gen")), "right_count": st.get("right", 0),
            "wrong_count": st.get("wrong", 0), "skip_count": st.get("skip", 0),
            "uname": st.get("uname", ""), "updated_at": int(time.time())
        }).execute())
    except Exception as e:
        logger.error(f"[DB] save_last_quiz error: {e}")

async def db_get_last_quiz(uid: int) -> dict:
    try:
        r = await sb_exec(lambda: sb.table("quiz_last_state").select("*").eq("user_id", uid).execute())
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

