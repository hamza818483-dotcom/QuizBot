# ============================================================
# ATLAS BOT — PDF Handler
# PDF → Images → Gemini MCQ Generation + OpenRouter Fallback
# ============================================================

import os
import re
import json
import logging
import random
import base64
import asyncio
import time
import contextvars
import hashlib
from io import BytesIO
from PIL import Image

# Global queue lock: ensures only ONE MCQ-generation job (image or text)
# runs at a time across the entire bot, regardless of which command
# triggered it (/img, /pdf, /csv, quiz-master, etc). Other bot commands
# (menu, settings, admin, etc.) never touch this lock and stay fully parallel.
MCQ_PROCESSING_QUEUE_LOCK = asyncio.Lock()

import httpx

try:
    from core import BD_TZ
except Exception:
    BD_TZ = None

try:
    from zoneinfo import ZoneInfo
    GEMINI_QUOTA_TZ = ZoneInfo("America/Los_Angeles")  # Google free-tier quota resets at Pacific midnight
except Exception:
    GEMINI_QUOTA_TZ = None

logger = logging.getLogger("atlas.pdf_handler")

# ============================================================
# GEMINI KEY ROTATION
# ============================================================
_gemini_key_exhausted_day: dict = {}   # key -> 'YYYY-MM-DD' (Pacific) it was marked exhausted
_gemini_key_exhausted_flag: dict = {}  # key -> True while exhausted-today
_gemini_exhausted_d1_loaded = False    # set True once startup rehydrate has run

def _gemini_key_hash(key: str) -> str:
    """Short, stable, non-reversible identifier for a key -- stored in D1
    instead of the raw key so the exhausted-keys table never holds live
    credentials."""
    return hashlib.sha256(key.encode()).hexdigest()[:24]

def _gemini_quota_today_str() -> str:
    from datetime import datetime
    if GEMINI_QUOTA_TZ is not None:
        return datetime.now(GEMINI_QUOTA_TZ).strftime('%Y-%m-%d')
    return datetime.utcnow().strftime('%Y-%m-%d')

async def load_gemini_exhausted_keys_from_d1():
    """Call once at bot startup, after key_rotator is constructed. Rehydrates
    the in-memory exhausted-today state from D1 so a restart doesn't lose
    it -- without this, /keys always shows every key "healthy" right after
    a restart even if Google's actual daily quota for those keys is still
    exhausted, and the bot wastes a fresh 429 round-trip re-discovering
    what it already knew before restarting."""
    global _gemini_exhausted_d1_loaded
    try:
        from core import db_load_gemini_exhausted_keys
        today = _gemini_quota_today_str()
        rows = await db_load_gemini_exhausted_keys()
        restored = 0
        for key in key_rotator.keys:
            h = _gemini_key_hash(key)
            day = rows.get(h)
            if day == today:
                _gemini_key_exhausted_day[key] = day
                _gemini_key_exhausted_flag[key] = True
                restored += 1
        if restored:
            logger.warning(f"[Gemini] Restored {restored} daily-exhausted key(s) from D1 after restart")
        else:
            logger.info(f"[Gemini] D1 rehydrate ran: 0 keys were marked exhausted-today in D1 (either all quotas are genuinely fresh, or D1 write never happened for yesterday's exhausted keys — check [D1] warnings in logs if this seems wrong)")
        _gemini_exhausted_d1_loaded = True
    except Exception as e:
        logger.warning(f"[Gemini] load_gemini_exhausted_keys_from_d1 failed (non-fatal, starts fresh): {e}")

async def load_key_warmup_state_from_d1():
    """Call once at bot startup, after key_rotator is constructed. Rehydrates
    each key's first-seen timestamp from D1 so the warm-up clock survives a
    restart -- without this, every restart would make every key look
    'brand new' again and re-throttle keys that had already earned full
    trust days ago."""
    try:
        from core import db_load_key_first_seen
        rows = await db_load_key_first_seen()  # {key_hash: first_seen_at}
        restored = 0
        for key in key_rotator.keys:
            h = _gemini_key_hash(key)
            ts = rows.get(h)
            if ts:
                key_rotator._key_first_seen[key] = ts
                restored += 1
        still_new = [k for k in key_rotator.keys if key_rotator.is_warming_up(k)]
        logger.info(f"[Gemini] Warm-up state restored for {restored}/{len(key_rotator.keys)} key(s) from D1; {len(still_new)} key(s) still inside the {GeminiKeyRotator.WARMUP_DAYS}-day warm-up window")
    except Exception as e:
        logger.warning(f"[Gemini] load_key_warmup_state_from_d1 failed (non-fatal, treats all keys as warmed-up): {e}")


async def load_banned_keys_from_d1():
    """Call once at bot startup, after key_rotator is constructed. The local
    /tmp ban file is wiped on every restart (routine on free-tier hosting) --
    this rehydrates from D1 so a permanently-banned key (403 suspended, 401
    invalid/deleted service account) doesn't silently come back into
    rotation after a restart and get retried against an already-flagged
    Google account."""
    try:
        from core import db_load_gemini_banned_keys
        rows = await db_load_gemini_banned_keys()  # {key_hash: {reason, banned_at, key_age_days_at_ban}}
        if not rows:
            return
        by_hash = {_gemini_key_hash(k): k for k in key_rotator.keys}
        restored = 0
        for h, meta in rows.items():
            key = by_hash.get(h)
            if not key:
                continue  # key no longer configured (removed from env) -- nothing to re-ban
            key_rotator._banned.add(key)
            if meta.get("reason"):
                key_rotator._ban_reasons[key] = meta["reason"]
            key_rotator._ban_meta[key] = {"banned_at": meta.get("banned_at"),
                                           "key_age_days_at_ban": meta.get("key_age_days_at_ban")}
            restored += 1
        if restored:
            key_rotator.keys = [k for k in key_rotator.keys if k not in key_rotator._banned]
            _save_banned_keys(key_rotator._banned)
            _save_banned_reasons(key_rotator._ban_reasons)
            _save_ban_meta(key_rotator._ban_meta)
            logger.warning(f"[Gemini] Re-applied {restored} permanent ban(s) from D1 after restart ({len(key_rotator.keys)} usable keys remain)")
    except Exception as e:
        logger.warning(f"[Gemini] load_banned_keys_from_d1 failed (non-fatal, relies on local /tmp file only): {e}")


def _is_gemini_key_exhausted_today(key: str) -> bool:
    """Daily quota-exhaustion memory for Gemini free-tier keys
    (20 requests/day/model). A 60s cooldown is pointless for a daily quota —
    the key stays dead until the quota resets, so once it 429s with a
    quota/RESOURCE_EXHAUSTED error, skip it until Google's actual reset
    (Pacific midnight) instead of re-trying it every call."""
    today = _gemini_quota_today_str()
    if _gemini_key_exhausted_day.get(key) != today:
        _gemini_key_exhausted_day[key] = today
        _gemini_key_exhausted_flag[key] = False
    return _gemini_key_exhausted_flag.get(key, False)


def _gemini_full_error_text(e: Exception) -> str:
    """str(e) from the google-genai SDK often omits the nested quota-detail
    JSON (quotaId/'PerDay' text lives in the raw HTTP response body, not
    always in the exception's own __str__). Without the full body, the
    'PerDay'/'generate_content_free_tier_requests' substring check below
    can miss a genuine DAILY quota exhaustion and only apply the 60s
    short-cooldown instead — meaning the key comes back on the next call
    (and shows 'healthy' again after any restart) even though Google's
    real daily quota for it is still exhausted for the rest of the day.
    This pulls response.text/response.body (whichever the SDK's exception
    exposes) and appends it so the daily-quota substring check has the
    real data to match against, same pattern already used by
    _qbm_gemini_raw_multi/_dagano_gemini_raw_multi in app.py."""
    msg = str(e)
    extra = ""
    try:
        resp_obj = getattr(e, "response", None)
        if resp_obj is not None:
            extra = getattr(resp_obj, "text", "") or ""
            if not extra:
                body = getattr(resp_obj, "body", None)
                if body:
                    extra = body if isinstance(body, str) else str(body)
    except Exception:
        pass
    if not extra:
        try:
            args_text = " ".join(str(a) for a in getattr(e, "args", []))
            if args_text and args_text != msg:
                extra = args_text
        except Exception:
            pass
    return (msg + " " + extra).strip() if extra else msg

def _mark_gemini_key_exhausted_today(key: str):
    today = _gemini_quota_today_str()
    _gemini_key_exhausted_day[key] = today
    _gemini_key_exhausted_flag[key] = True
    try:
        import asyncio
        from core import db_mark_gemini_key_exhausted
        asyncio.create_task(db_mark_gemini_key_exhausted(_gemini_key_hash(key), today))
    except Exception as e:
        logger.warning(f"[Gemini] D1 persist exhausted-mark failed (non-fatal, in-memory state still correct): {e}")

def _mark_gemini_key_healthy_today(key: str):
    _gemini_key_exhausted_flag[key] = False
    try:
        import asyncio
        from core import db_mark_gemini_key_healthy
        asyncio.create_task(db_mark_gemini_key_healthy(_gemini_key_hash(key)))
    except Exception as e:
        logger.warning(f"[Gemini] D1 clear exhausted-mark failed (non-fatal, in-memory state still correct): {e}")


_BANNED_KEYS_FILE = "/tmp/atlas_banned_gemini_keys.json"
_BANNED_REASONS_FILE = "/tmp/atlas_banned_gemini_reasons.json"

def _load_banned_keys() -> set:
    try:
        with open(_BANNED_KEYS_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def _save_banned_keys(banned: set):
    try:
        with open(_BANNED_KEYS_FILE, "w") as f:
            json.dump(list(banned), f)
    except Exception as e:
        logger.warning(f"[Gemini] Failed to persist banned keys: {e}")

def _load_banned_reasons() -> dict:
    try:
        with open(_BANNED_REASONS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_banned_reasons(reasons: dict):
    try:
        with open(_BANNED_REASONS_FILE, "w") as f:
            json.dump(reasons, f)
    except Exception as e:
        logger.warning(f"[Gemini] Failed to persist banned reasons: {e}")

_BAN_META_FILE = "/tmp/atlas_banned_gemini_meta.json"

def _load_ban_meta() -> dict:
    try:
        with open(_BAN_META_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_ban_meta(meta: dict):
    try:
        with open(_BAN_META_FILE, "w") as f:
            json.dump(meta, f)
    except Exception as e:
        logger.warning(f"[Gemini] Failed to persist ban metadata: {e}")


class GeminiKeyRotator:
    COOLDOWN_SECONDS = 60
    RPM_PER_KEY = 15  # proactive per-minute ceiling; skip a key before it 429s
    RPM_WINDOW_SECONDS = 60

    ACCOUNT_CONCURRENT_CAP = 2  # only takes effect for keys explicitly tagged
    # via GEMINI_KEYS_GROUPED (key@account). Untagged keys each count as
    # their own account, so this provides no protection by itself when
    # grouping is unknown -- see GLOBAL_CONCURRENT_CAP below for the
    # mapping-independent safeguard.

    GLOBAL_CONCURRENT_CAP = 8  # hard ceiling on simultaneous in-flight Gemini
    # calls across ALL keys/accounts combined, regardless of grouping info.
    # Lowered from 20 -> 8 after real suspensions were observed (2-3
    # projects per account) even under per-key RPM limits -- the burst
    # *volume* itself, not just per-key rate, appears to be part of what's
    # flagged. Slower throughput, but priority is zero further suspensions.
    GLOBAL_MIN_GAP_SECONDS = 0.25  # minimum spacing enforced between any two
    # Gemini calls starting, globally -- raised from 0.05 -> 0.25 for the
    # same reason: spreads call-starts out in time so concurrent load never
    # looks like an instantaneous fan-out burst.

    DISTINCT_ACCOUNT_CONCURRENT_CAP = 3  # hard ceiling on how many DIFFERENT
    # accounts can have an in-flight call at the same instant, independent of
    # GLOBAL_CONCURRENT_CAP (which only bounds total call count, not account
    # diversity). All keys/accounts share one egress IP here, so N different
    # "accounts" firing simultaneously from that IP is itself a correlatable
    # fingerprint (looks like one operator running N accounts in parallel,
    # not N independent users). Capping simultaneous distinct accounts keeps
    # the live account-set small at any given instant even with many
    # accounts configured overall.

    ACCOUNT_MIN_GAP_SECONDS = 1.0  # minimum spacing between call-starts on
    # the SAME account, tighter than the global gap. Two different accounts
    # can still fire close together (global gap covers that), but repeated
    # hits on one account are spread out further -- reduces same-account
    # burst signature even when the account is under its concurrency cap.

    ACCOUNT_DAILY_CALL_CAP = 1200  # soft ceiling on total calls per account
    # per rolling 24h. Free-tier keys used at sustained bot/commercial-scale
    # volume is itself a suspension trigger regardless of per-minute/burst
    # shaping -- this caps the aggregate daily footprint per Google account
    # so no single account's total usage pattern looks like production-scale
    # traffic riding a free tier. When an account crosses this, its keys are
    # pushed to the back of ordered_keys() (not hard-blocked) so load drains
    # to less-used accounts first.
    ACCOUNT_ERROR_CIRCUIT_THRESHOLD = 4  # consecutive 401/403/suspend-style
    # errors from the SAME account (across any of its keys) within
    # ACCOUNT_ERROR_CIRCUIT_WINDOW triggers a temporary full-account pause --
    # this is the code-side approximation of "account looks flagged, back off
    # entirely" rather than continuing to round-robin its other keys, which
    # previously kept hitting an account that was already being penalized.
    ACCOUNT_ERROR_CIRCUIT_WINDOW = 300  # seconds
    ACCOUNT_ERROR_CIRCUIT_COOLDOWN = 1800  # 30 min base pause on 1st trip

    # ── EXPONENTIAL BACKOFF ON REPEAT TRIPS ─────────────────────────────
    # A single trip is treated as noise (transient/network). An account
    # that keeps re-tripping soon after each cooldown lifts is a stronger
    # signal something is actually wrong with it, so each repeat trip
    # (within CIRCUIT_BACKOFF_RESET_WINDOW of the previous one lifting)
    # multiplies the cooldown, up to a cap. A long clean stretch resets
    # the level back to 0 -- this is NOT a permanent downgrade.
    CIRCUIT_BACKOFF_MULTIPLIER = 4  # 30min -> 2hr -> 8hr (capped)
    CIRCUIT_BACKOFF_MAX_COOLDOWN = 21600  # 6hr hard cap
    CIRCUIT_BACKOFF_RESET_WINDOW = 86400  # 24hr clean (no new trip) resets level to 0

    # ── NEW-KEY WARM-UP ──────────────────────────────────────────────────
    # A brand-new key jumping straight into full rotation load (same RPM,
    # same concurrency as a key with months of history) is itself a signal
    # per the mass-ban review -- Google's abuse detection appears to weigh
    # a key/project's usage history, not just instantaneous rate. Any key
    # whose first-ever-seen timestamp is within WARMUP_DAYS gets a reduced
    # RPM ceiling and is excluded from the account concurrency pool,
    # ramping linearly up to full trust by day WARMUP_DAYS.
    WARMUP_DAYS = 5
    WARMUP_DAY0_RPM_FRACTION = 0.2  # day 0: only 20% of RPM_PER_KEY allowed
    WARMUP_MAX_CONCURRENT = 1  # a warming-up key never gets more than 1
    # simultaneous in-flight call, regardless of ACCOUNT_CONCURRENT_CAP.

    # ── PER-ACCOUNT STAGGERED KEY ROLLOUT ───────────────────────────────
    # Per-key warm-up alone doesn't cover the case of adding many NEW keys
    # to the SAME Google account all at once (e.g. pasting in all 10
    # freshly-created project keys for one account in a single env var
    # update) -- that's still an account-level anomaly (a burst of brand
    # new projects all activating together) even though each key
    # individually ramps its own RPM. To avoid this, keys within an
    # account are activated in a staggered order: only the first
    # ACCOUNT_STAGGER_BATCH_SIZE keys (by first-seen time) are usable
    # immediately; the next batch unlocks after ACCOUNT_STAGGER_DAYS, and
    # so on, until the whole account's key set is live.
    ACCOUNT_STAGGER_BATCH_SIZE = 4  # keys unlocked per stagger step -- larger
    # batch keeps more quota usable sooner (balance: not too slow), while
    # still avoiding a full 10-key account activating in one shot.
    ACCOUNT_STAGGER_DAYS = 1  # days between unlocking each stagger step --
    # short gap so a 10-key account reaches full quota within ~2-3 days
    # instead of a full week, while still breaking up the "all at once"
    # signal into 2-3 distinct activation events over time.

    def __init__(self):
        self.keys = []
        self.current = 0
        self._cooldown_until = {}
        self._banned = _load_banned_keys()
        self._ban_reasons = _load_banned_reasons()  # key -> reason string, for /keys visibility
        self._ban_meta = _load_ban_meta()  # key -> {banned_at, key_age_days_at_ban}, for timeline review
        self._call_times = {}  # key -> list[float] call timestamps (rolling 60s window)
        self._key_account = {}  # key -> account_id
        self._account_inflight = {}  # account_id -> current in-flight count
        self._account_last_call = {}  # account_id -> last call-start time
        self._account_daily_calls = {}  # account_id -> list[float] call timestamps (rolling 24h)
        self._account_error_times = {}  # account_id -> list[float] recent error timestamps
        self._account_circuit_until = {}  # account_id -> epoch time when pause lifts
        self._account_backoff_level = {}  # account_id -> consecutive-trip count (resets after a clean window)
        self._account_last_trip_lifted = {}  # account_id -> epoch time the last cooldown lifted
        self._distinct_accounts_inflight = set()  # account_ids currently holding a slot
        self._distinct_account_cond = None  # asyncio.Condition, created lazily (needs running loop)
        self._account_slot_cond = None  # asyncio.Condition for ACCOUNT_CONCURRENT_CAP hard-enforcement
        self._global_sem = asyncio.Semaphore(self.GLOBAL_CONCURRENT_CAP)
        self._global_lock = asyncio.Lock()
        self._last_global_call = 0.0
        self._key_first_seen = {}  # key -> unix ts first observed (D1-backed, rehydrated at startup)
        self._key_inflight = {}  # key -> current in-flight count (used only during warm-up)
        self._key_daily_calls = {}  # key -> list[float] rolling-24h call timestamps, for within-account balance
        self._load_keys()

    def warmup_days_elapsed(self, key: str) -> float:
        """Days since this key was first seen. Returns WARMUP_DAYS (i.e.
        'fully warmed up') if first-seen is unknown, so a key never gets
        throttled just because D1 rehydrate hasn't run/loaded yet -- safer
        default is normal treatment, not silently starving an old key."""
        first_seen = self._key_first_seen.get(key)
        if not first_seen:
            return float(self.WARMUP_DAYS)
        return (time.time() - first_seen) / 86400.0

    def is_warming_up(self, key: str) -> bool:
        return self.warmup_days_elapsed(key) < self.WARMUP_DAYS

    def warmup_rpm_limit(self, key: str) -> int:
        """Linearly ramps from WARMUP_DAY0_RPM_FRACTION*RPM_PER_KEY on day 0
        up to the full RPM_PER_KEY by WARMUP_DAYS."""
        days = self.warmup_days_elapsed(key)
        if days >= self.WARMUP_DAYS:
            return self.RPM_PER_KEY
        progress = max(0.0, days) / self.WARMUP_DAYS  # 0.0 .. 1.0
        fraction = self.WARMUP_DAY0_RPM_FRACTION + progress * (1.0 - self.WARMUP_DAY0_RPM_FRACTION)
        return max(1, int(self.RPM_PER_KEY * fraction))

    def note_key_seen(self, key: str):
        """Marks a key as seen right now if it has never been recorded
        before (in-memory + fire-and-forget D1 persist). Safe to call on
        every use -- D1 insert is INSERT...ON CONFLICT DO NOTHING so this
        never moves an existing first-seen date forward."""
        if key not in self._key_first_seen:
            self._key_first_seen[key] = time.time()
        try:
            from core import db_record_key_first_seen
            asyncio.create_task(db_record_key_first_seen(_gemini_key_hash(key), "gemini"))
        except Exception as e:
            logger.warning(f"[Gemini] note_key_seen D1 persist warn (non-fatal): {e}")

    def is_stagger_locked(self, key: str) -> bool:
        """Returns True if this key belongs to an account whose keys are
        being rolled out in stagger batches (ordered by first-seen time
        within the account) and this particular key hasn't reached its
        unlock step yet. A key with unknown first-seen is treated as
        already unlocked (same fail-safe default as warm-up: never starve
        a key just because D1 rehydrate hasn't run)."""
        acct = self.account_of(key)
        siblings = [k for k in self.keys if self.account_of(k) == acct]
        if len(siblings) <= self.ACCOUNT_STAGGER_BATCH_SIZE:
            return False  # small accounts never need staggering
        # Order siblings by first-seen time (unknown/never-seen keys sort
        # last, since they haven't been used yet so their "activation"
        # naturally happens whenever they're first picked).
        def _fs(k):
            return self._key_first_seen.get(k, float("inf"))
        ordered = sorted(siblings, key=_fs)
        idx = ordered.index(key)
        step = idx // self.ACCOUNT_STAGGER_BATCH_SIZE
        if step == 0:
            return False  # first batch always unlocked
        # This step unlocks ACCOUNT_STAGGER_DAYS * step days after the
        # ACCOUNT's own first key was first seen (i.e. the account's
        # activation start), not after this specific key -- otherwise an
        # unused key sitting at the back of the list would never age in.
        acct_start = min((self._key_first_seen.get(k, time.time()) for k in siblings), default=time.time())
        unlock_at = acct_start + step * self.ACCOUNT_STAGGER_DAYS * 86400
        return time.time() < unlock_at

    def _prune_account_daily(self, acct: str, now: float) -> int:
        times = self._account_daily_calls.get(acct)
        if not times:
            return 0
        cutoff = now - 86400
        fresh = [t for t in times if t > cutoff]
        self._account_daily_calls[acct] = fresh
        return len(fresh)

    def record_account_call(self, key: str):
        """Call alongside record_call() so per-account daily volume is
        tracked independent of per-key RPM tracking."""
        acct = self.account_of(key)
        self._account_daily_calls.setdefault(acct, []).append(time.time())

    def account_over_daily_cap(self, key: str) -> bool:
        acct = self.account_of(key)
        return self._prune_account_daily(acct, time.time()) >= self.ACCOUNT_DAILY_CALL_CAP

    def record_account_error(self, key: str):
        """Track suspend/auth-style errors per account. If enough pile up in
        the window, trip a full-account circuit breaker so the code stops
        routing ANY of that account's keys for a cooldown period, instead of
        continuing to cycle through its other keys (which just spreads the
        same flagged-account risk across more of its own quota)."""
        acct = self.account_of(key)
        now = time.time()
        times = [t for t in self._account_error_times.get(acct, []) if t > now - self.ACCOUNT_ERROR_CIRCUIT_WINDOW]
        times.append(now)
        self._account_error_times[acct] = times
        if len(times) >= self.ACCOUNT_ERROR_CIRCUIT_THRESHOLD:
            # Decide backoff level: if the previous cooldown lifted recently
            # (within CIRCUIT_BACKOFF_RESET_WINDOW), this is a repeat trip --
            # escalate. Otherwise treat as a fresh, isolated incident.
            last_lifted = self._account_last_trip_lifted.get(acct, 0)
            if last_lifted and (now - last_lifted) < self.CIRCUIT_BACKOFF_RESET_WINDOW:
                level = self._account_backoff_level.get(acct, 0) + 1
            else:
                level = 0  # clean stretch since last trip -- reset to base
            self._account_backoff_level[acct] = level
            cooldown = min(
                self.ACCOUNT_ERROR_CIRCUIT_COOLDOWN * (self.CIRCUIT_BACKOFF_MULTIPLIER ** level),
                self.CIRCUIT_BACKOFF_MAX_COOLDOWN,
            )
            self._account_circuit_until[acct] = now + cooldown
            logger.error(f"[Gemini] Account circuit breaker TRIPPED for {acct}: {len(times)} errors in {self.ACCOUNT_ERROR_CIRCUIT_WINDOW}s -- backoff level {level}, pausing this account's keys for {cooldown}s")

    def account_circuit_open(self, key: str) -> bool:
        acct = self.account_of(key)
        until = self._account_circuit_until.get(acct, 0)
        if until and time.time() >= until:
            del self._account_circuit_until[acct]
            self._account_last_trip_lifted[acct] = time.time()  # marks when the clean-window clock starts
            return False
        return bool(until)


    def _load_keys(self):
        # Preferred #1: separate env vars per account, e.g.
        #   GEMINI_KEYS_ACC1=key1,key2,key3
        #   GEMINI_KEYS_ACC2=key4,key5,key6
        # Easiest option for the user -- no need to tag each key inline,
        # just put each account's keys in their own named env var.
        all_keys = []
        self._key_account = {}
        acc_env_vars = sorted(
            k for k in os.environ.keys()
            if k.startswith("GEMINI_KEYS_ACC") or k.startswith("GEMINI_KEYS_ACCOUNT")
        )
        if acc_env_vars:
            for env_name in acc_env_vars:
                acct = env_name  # e.g. "GEMINI_KEYS_ACC1" used directly as account tag
                for entry in os.environ.get(env_name, "").split(","):
                    key = entry.strip()
                    if not key:
                        continue
                    all_keys.append(key)
                    self._key_account[key] = acct
            n_accounts = len(acc_env_vars)
            logger.info(f"[Gemini] Loaded {len(all_keys)} keys from {n_accounts} per-account env var(s): {acc_env_vars}")
            self.keys = [k for k in all_keys if k not in self._banned]
            skipped = [k for k in all_keys if k in self._banned]
            if skipped:
                logger.warning(f"[Gemini] Skipped {len(skipped)} previously-banned key(s) at startup: {[k[:12]+'...' for k in skipped]}")
            logger.info(f"[Gemini] Loaded {len(self.keys)} usable keys across {n_accounts} account(s) ({len(skipped)} auto-skipped as banned)")
            return
        # Preferred #2: GEMINI_KEYS_GROUPED="key1@acct1,key2@acct1,key3@acct2,..."
        # explicitly tags which Google account each key belongs to, so keys
        # from the same account can be spread apart instead of hammered
        # together. Falls back to plain GEMINI_KEYS (no @account suffix)
        # where every key is treated as its own account -- same behavior as
        # before, just without cross-account spreading.
        raw = os.environ.get("GEMINI_KEYS_GROUPED", "") or os.environ.get("GEMINI_KEYS", "")
        explicit_tagging = "@" in raw
        if raw:
            for entry in raw.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                if "@" in entry:
                    key, acct = entry.rsplit("@", 1)
                    key = key.strip()
                    acct = acct.strip() or key
                else:
                    key, acct = entry, entry
                all_keys.append(key)
                self._key_account[key] = acct
        # AUTO-BATCH FALLBACK: when no key carries an explicit "@account" tag
        # (plain GEMINI_KEYS list), every key was being treated as its own
        # account, so ACCOUNT_CONCURRENT_CAP never engaged -- N keys from the
        # same real Google account could all get hit at once, which is the
        # likely cause of the account-level suspensions seen (2-3 projects
        # per account). Keys are typically appended to the env var in the
        # same order they were created (10 keys/account from the "10
        # projects per account" pattern), so grouping every consecutive
        # AUTO_BATCH_SIZE keys into one virtual account approximates the
        # real grouping without needing the user to remember or re-tag
        # anything. This costs nothing when the mapping happens to be
        # wrong (worst case: same behavior as today, no @-tags) and helps
        # whenever the order does line up with real account boundaries.
        if all_keys and not explicit_tagging:
            # Two zones per user-provided clue: first FIRST_ZONE_SIZE keys
            # have unknown/mixed grouping (treated as individual accounts --
            # safe default, same as before), keys after that are serially
            # ZONE2_BATCH_SIZE per account (8-9 confirmed by user; using 9
            # errs toward under-grouping rather than over-grouping two
            # different real accounts into one, which would over-restrict
            # unrelated accounts).
            FIRST_ZONE_SIZE = int(os.environ.get("GEMINI_FIRST_ZONE_SIZE", "44"))
            ZONE2_BATCH_SIZE = int(os.environ.get("GEMINI_ZONE2_BATCH_SIZE", "9"))
            for idx, key in enumerate(all_keys):
                if idx < FIRST_ZONE_SIZE:
                    self._key_account[key] = f"zone1-key-{idx}"
                else:
                    zone2_idx = idx - FIRST_ZONE_SIZE
                    self._key_account[key] = f"zone2-batch-{zone2_idx // ZONE2_BATCH_SIZE}"
            logger.info(f"[Gemini] No @account tags found — auto-grouped keys: first {FIRST_ZONE_SIZE} treated individually, remaining {len(all_keys) - FIRST_ZONE_SIZE} grouped in batches of {ZONE2_BATCH_SIZE} by env-var order")
        skipped = [k for k in all_keys if k in self._banned]
        self.keys = [k for k in all_keys if k not in self._banned]
        n_accounts = len(set(self._key_account.get(k, k) for k in self.keys))
        if skipped:
            logger.warning(f"[Gemini] Skipped {len(skipped)} previously-banned key(s) at startup: {[k[:12]+'...' for k in skipped]}")
        logger.info(f"[Gemini] Loaded {len(self.keys)} usable keys across {n_accounts} account(s) ({len(skipped)} auto-skipped as banned)")

    def account_of(self, key: str) -> str:
        return self._key_account.get(key, key)

    def acquire_account_slot(self, key: str) -> bool:
        """Returns True and reserves a slot if this key's account is under
        its concurrent-in-flight cap; False if the account is already
        saturated (caller should skip to the next candidate key)."""
        acct = self.account_of(key)
        n = self._account_inflight.get(acct, 0)
        if n >= self.ACCOUNT_CONCURRENT_CAP:
            return False
        self._account_inflight[acct] = n + 1
        return True

    def release_account_slot(self, key: str):
        acct = self.account_of(key)
        n = self._account_inflight.get(acct, 0)
        self._account_inflight[acct] = max(0, n - 1)

    def _get_account_slot_cond(self) -> asyncio.Condition:
        if self._account_slot_cond is None:
            self._account_slot_cond = asyncio.Condition()
        return self._account_slot_cond

    async def acquire_account_slot_blocking(self, key: str):
        """Hard-blocking version of acquire_account_slot: waits indefinitely
        (no timeout/give-up) until this key's account has a free concurrent
        slot under ACCOUNT_CONCURRENT_CAP. The old acquire_account_slot's
        caller gave up after ~2s and proceeded anyway, which meant the cap
        was cosmetic under real load -- a 5-key account WOULD end up with
        more than ACCOUNT_CONCURRENT_CAP calls in flight simultaneously
        whenever the 2s wait elapsed. This guarantees the cap actually
        holds, at the cost of calls queueing longer under heavy same-account
        load -- the correct trade-off given the whole point of the cap is
        exactly this kind of enforcement."""
        cond = self._get_account_slot_cond()
        async with cond:
            while not self.acquire_account_slot(key):
                await cond.wait()

    async def release_account_slot_blocking(self, key: str):
        cond = self._get_account_slot_cond()
        async with cond:
            self.release_account_slot(key)
            cond.notify_all()

    def _get_distinct_account_cond(self) -> asyncio.Condition:
        if self._distinct_account_cond is None:
            self._distinct_account_cond = asyncio.Condition()
        return self._distinct_account_cond

    async def acquire_distinct_account_slot(self, acct: str):
        """Blocks until fewer than DISTINCT_ACCOUNT_CONCURRENT_CAP distinct
        accounts are currently in-flight, OR this account already holds a
        slot (re-entrant per account -- multiple calls on the SAME account
        don't count against account diversity, only different accounts do).
        Caller must pair with release_distinct_account_slot in a finally."""
        cond = self._get_distinct_account_cond()
        async with cond:
            while (acct not in self._distinct_accounts_inflight
                   and len(self._distinct_accounts_inflight) >= self.DISTINCT_ACCOUNT_CONCURRENT_CAP):
                await cond.wait()
            self._distinct_accounts_inflight.add(acct)

    async def release_distinct_account_slot(self, acct: str):
        cond = self._get_distinct_account_cond()
        async with cond:
            # Only drop the account once nothing else on it is still
            # in-flight -- tracked via _account_inflight (already maintained
            # by acquire_account_slot/release_account_slot).
            if self._account_inflight.get(acct, 0) <= 0:
                self._distinct_accounts_inflight.discard(acct)
            cond.notify_all()

    class _ThrottleGuard:
        def __init__(self, rotator, key=None):
            self.rotator = rotator
            self.key = key
            self._got_account_slot = False
            self._got_warmup_slot = False
            self._got_distinct_slot = None
        async def __aenter__(self):
            r = self.rotator
            # Warm-up concurrency gate: a still-warming key is capped at
            # WARMUP_MAX_CONCURRENT in-flight calls regardless of the
            # account/global caps, so a brand-new key never gets swept into
            # a multi-call burst on its very first days of use.
            if self.key is not None and r.is_warming_up(self.key):
                for _ in range(30):  # ~3s max wait before giving up the gate
                    n = r._key_inflight.get(self.key, 0)
                    if n < r.WARMUP_MAX_CONCURRENT:
                        r._key_inflight[self.key] = n + 1
                        self._got_warmup_slot = True
                        break
                    await asyncio.sleep(0.1)
            await r._global_sem.acquire()
            async with r._global_lock:
                now = time.time()
                # jitter added so call spacing isn't perfectly uniform
                # (uniform inter-call timing is itself a bot-detection signal)
                jittered_gap = r.GLOBAL_MIN_GAP_SECONDS + random.uniform(0.0, r.GLOBAL_MIN_GAP_SECONDS * 0.6)
                wait = jittered_gap - (now - r._last_global_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                r._last_global_call = time.time()
            # Account-level gate: hard-blocking (acquire_account_slot_blocking)
            # so ACCOUNT_CONCURRENT_CAP is a real ceiling -- previously this
            # waited ~2s and proceeded anyway on timeout, meaning a busy
            # account (e.g. 5 keys all healthy, heavy load) could still end
            # up with MORE than ACCOUNT_CONCURRENT_CAP calls in flight at
            # once. Now it queues until a slot is genuinely free.
            if self.key is not None:
                acct = r.account_of(self.key)
                await r.acquire_distinct_account_slot(acct)
                self._got_distinct_slot = acct
                async with r._global_lock:
                    now = time.time()
                    last = r._account_last_call.get(acct, 0.0)
                    jittered_acct_gap = r.ACCOUNT_MIN_GAP_SECONDS + random.uniform(0.0, r.ACCOUNT_MIN_GAP_SECONDS * 0.5)
                    wait = jittered_acct_gap - (now - last)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    r._account_last_call[acct] = time.time()
                await r.acquire_account_slot_blocking(self.key)
                self._got_account_slot = True
            return self
        async def __aexit__(self, exc_type, exc, tb):
            if self._got_account_slot:
                await self.rotator.release_account_slot_blocking(self.key)
            if self._got_distinct_slot is not None:
                await self.rotator.release_distinct_account_slot(self._got_distinct_slot)
            if self._got_warmup_slot:
                n = self.rotator._key_inflight.get(self.key, 0)
                self.rotator._key_inflight[self.key] = max(0, n - 1)
            self.rotator._global_sem.release()
            return False

    def throttled_call(self, key: str = None):
        """Async context manager: `async with rotator.throttled_call(key=key): ...`
        Caps total simultaneous Gemini calls at GLOBAL_CONCURRENT_CAP,
        enforces a minimum spacing between call starts, and (when `key` is
        given) also gates on ACCOUNT_CONCURRENT_CAP so no single Google
        account gets more than 2 simultaneous in-flight calls across its
        keys. Wrap the actual Gemini API call site with it. `key` is
        optional for backward compatibility with existing call sites that
        don't pass it (they still get the global cap+spacing, just not the
        account gate)."""
        return self._ThrottleGuard(self, key=key)

    def _prune_and_count(self, key: str, now: float) -> int:
        """Drops call-timestamps older than the rolling window and returns
        the remaining count for this key."""
        times = self._call_times.get(key)
        if not times:
            return 0
        cutoff = now - self.RPM_WINDOW_SECONDS
        fresh = [t for t in times if t > cutoff]
        self._call_times[key] = fresh
        return len(fresh)

    def record_call(self, key: str):
        """Call this right before/when actually using a key, so the rolling
        window reflects real usage (independent of mark_healthy/rate_limited)."""
        self._call_times.setdefault(key, []).append(time.time())
        self.record_account_call(key)
        self.note_key_seen(key)
        now = time.time()
        cutoff = now - 86400
        times = [t for t in self._key_daily_calls.get(key, []) if t > cutoff]
        times.append(now)
        self._key_daily_calls[key] = times

    def key_daily_call_count(self, key: str) -> int:
        """Rolling-24h call count for THIS key specifically (not the whole
        account) -- used to balance usage across an account's own keys so
        random shuffle doesn't let a couple of keys silently absorb most of
        an account's traffic while siblings sit near-idle."""
        now = time.time()
        cutoff = now - 86400
        times = [t for t in self._key_daily_calls.get(key, []) if t > cutoff]
        self._key_daily_calls[key] = times
        return len(times)

    def get_key(self):
        if not self.keys:
            raise ValueError("No Gemini keys available")
        key = self.keys[self.current % len(self.keys)]
        self.current = (self.current + 1) % len(self.keys)
        return key

    def ordered_keys(self, offset: int = 0):
        """Only non-banned keys, healthy ones first: not exhausted-today AND
        not in short cooldown AND not over its rolling per-minute ceiling,
        then over-RPM keys, then short-cooldown keys, then today-exhausted
        keys last — so a call never wastes its first attempt on a key
        already known dead for the day (daily quota resets at Pacific midnight,
        not after 60s), or one about to 429 from hitting its per-minute limit.

        Within the healthy tier, starts from self.current (advanced by
        get_key(), also nudged here) instead of always self.keys[0] --
        without this, every single call started its search from the same
        first key, so that key alone absorbed the first attempt of nearly
        every request and burned through its daily quota far faster than
        the other 35, even though total load was fairly distributed
        overall. Round-robining the healthy tier spreads first-attempts
        evenly across all live keys.

        `offset` lets QBM's parallel page-window give each concurrent slot
        its own starting key (same pattern as Groq's ordered_keys(offset=))
        -- without it, N pages running concurrently all read the same
        self.current snapshot before any of their awaits land, so they'd
        all pick the SAME "healthiest" key and hammer it at once instead of
        spreading across the pool, causing avoidable 429s mid-page for
        Gemini specifically (which now runs Call1 + miss-check + verify,
        3x the Gemini load per page QBM had before)."""
        now = time.time()
        live_keys = [k for k in self.keys if k not in self._banned]
        # Account circuit breaker: keys on a currently-paused account are
        # pushed to the very back (still usable as last resort, never
        # hard-blocked, since misfires shouldn't strand a request with zero
        # keys) -- this is checked before daily-cap/exhaustion tiers so a
        # tripped account never gets picked while any other option exists.
        circuit_open = [k for k in live_keys if self.account_circuit_open(k)]
        live_keys = [k for k in live_keys if k not in circuit_open]
        # Account stagger: keys in an unlocked-later batch (see
        # is_stagger_locked) are pushed to the very back too -- still
        # usable as last resort so a request never strands with zero keys,
        # but never picked while any unlocked key exists.
        stagger_locked = [k for k in live_keys if self.is_stagger_locked(k)]
        live_keys = [k for k in live_keys if k not in stagger_locked]
        not_exhausted = [k for k in live_keys if not _is_gemini_key_exhausted_today(k)]
        exhausted = [k for k in live_keys if _is_gemini_key_exhausted_today(k)]
        pool = not_exhausted if not_exhausted else live_keys
        cooled = [k for k in pool if self._cooldown_until.get(k, 0) <= now]
        cooling = [k for k in pool if self._cooldown_until.get(k, 0) > now]
        under_rpm = [k for k in cooled if self._prune_and_count(k, now) < self.warmup_rpm_limit(k)]
        over_rpm = [k for k in cooled if self._prune_and_count(k, now) >= self.warmup_rpm_limit(k)]
        # Daily account-volume cap: within the under-rpm tier, keys whose
        # account has already made ACCOUNT_DAILY_CALL_CAP+ calls today are
        # deprioritized (not blocked) below keys on less-used accounts, so
        # aggregate load naturally drains toward accounts with daily
        # headroom instead of concentrating sustained volume on a few.
        under_cap = [k for k in under_rpm if not self.account_over_daily_cap(k)]
        over_cap = [k for k in under_rpm if self.account_over_daily_cap(k)]
        healthy = under_cap
        if healthy:
            # Randomized start instead of pure sequential round-robin.
            # Deterministic serial rotation across an account's keys is
            # itself an abuse signal Google's suspension system watches
            # for (predictable ordered key-cycling from one account looks
            # scripted/hijacked even when total volume is within quota).
            # `offset` still separates concurrent slots so they don't
            # collide on the same key; `current` still advances so
            # sequential single-slot calls don't reuse one random pick
            # forever, but the actual starting point is randomized so the
            # observed key-use order isn't a fixed cycle.
            start = (self.current + offset + random.randint(0, len(healthy) - 1)) % len(healthy)
            healthy = healthy[start:] + healthy[:start]
            random.shuffle(healthy)
            # Within-account balance: group healthy keys by account, and
            # order accounts' own key-groups by each key's rolling-24h use
            # count (least-used-first). Pure random shuffle alone can let a
            # couple of an account's 10 keys absorb most of its traffic by
            # chance while siblings stay idle -- this doesn't remove the
            # cross-account randomization above (accounts/groups still
            # appear in the already-randomized order), it just makes sure
            # that WITHIN each account's own slice, its least-used key
            # surfaces first so load actually spreads across all N keys
            # instead of concentrating on whichever ones randomness favored.
            by_acct = {}
            order = []
            for k in healthy:
                acct = self.account_of(k)
                if acct not in by_acct:
                    by_acct[acct] = []
                    order.append(acct)
                by_acct[acct].append(k)
            rebuilt = []
            for acct in order:
                group = by_acct[acct]
                if len(group) > 1:
                    group.sort(key=lambda k: self.key_daily_call_count(k))
                rebuilt.extend(group)
            healthy = rebuilt
            self.current = (self.current + 1) % max(len(self.keys), 1)
        return healthy + over_cap + over_rpm + cooling + (exhausted if not_exhausted else []) + stagger_locked + circuit_open

    def ordered_keys_avoiding_accounts(self, avoid_accounts: set, offset: int = 0):
        """Same as ordered_keys(), but as a PURE tie-breaker within the
        top healthy tier only: among keys that are already equally
        "healthy" (not exhausted/cooling/over-cap/circuit-open), prefer
        ones whose account isn't in avoid_accounts. Never promotes a
        worse-tier key (cooling, over-cap, exhausted) ahead of a healthy
        one, and never demotes a healthy key below a worse tier -- doing
        so previously caused some calls to land on slower/cooling accounts
        first and eat into the per-call timeout budget, which showed up as
        more real extraction misses. This only changes ORDER WITHIN the
        healthy tier, so speed/success rate should match plain
        ordered_keys() while still nudging a single page's own sequential
        calls toward different accounts when the healthy tier has more
        than one to choose from."""
        base = self.ordered_keys(offset=offset)
        if not avoid_accounts or not base:
            return base
        now = time.time()
        live_keys = [k for k in self.keys if k not in self._banned]
        circuit_open = {k for k in live_keys if self.account_circuit_open(k)}
        not_exhausted = {k for k in live_keys if not _is_gemini_key_exhausted_today(k)}
        pool = not_exhausted if not_exhausted else set(live_keys)
        cooled = {k for k in pool if self._cooldown_until.get(k, 0) <= now}
        under_rpm = {k for k in cooled if self._prune_and_count(k, now) < self.warmup_rpm_limit(k)}
        under_cap = {k for k in under_rpm if not self.account_over_daily_cap(k)}
        healthy_set = under_cap - circuit_open
        healthy_set = {k for k in healthy_set if not self.is_stagger_locked(k)}
        # base's own prefix IS the healthy tier (see ordered_keys), so
        # intersecting with healthy_set recovers exactly that prefix
        # without re-deriving its (already-randomized) order.
        healthy_prefix_len = sum(1 for k in base if k in healthy_set)
        healthy_prefix = base[:healthy_prefix_len]
        rest = base[healthy_prefix_len:]
        preferred = [k for k in healthy_prefix if self.account_of(k) not in avoid_accounts]
        deprioritized = [k for k in healthy_prefix if self.account_of(k) in avoid_accounts]
        return (preferred + deprioritized if preferred else healthy_prefix) + rest

    def mark_rate_limited(self, key: str, daily_exhausted: bool = False, retry_after_seconds: int = None):
        cooldown = retry_after_seconds if retry_after_seconds and retry_after_seconds > 0 else self.COOLDOWN_SECONDS
        self._cooldown_until[key] = time.time() + cooldown
        if daily_exhausted:
            _mark_gemini_key_exhausted_today(key)

    def mark_banned(self, key: str, reason: str = ""):
        """Permanently skip this key (e.g. 403 CONSUMER_SUSPENDED / invalid key,
        401 UNAUTHENTICATED / ACCOUNT_STATE_INVALID) — removed from the active
        pool immediately and persisted to disk so restarts don't waste a retry
        cycle on a key that will keep failing forever. `reason` is stored so
        /keys can show WHY each key was banned, not just that it was. Also
        stores WHEN it was banned and how old the key was at ban-time (days
        since first_seen), so a later timeline review (was this a fresh key
        hit right after heavy first use, or an old established one?) doesn't
        require cross-referencing separate logs."""
        self._cooldown_until[key] = float("inf")
        self._banned.add(key)
        if reason:
            self._ban_reasons[key] = reason[:200]  # cap stored reason length
            _save_banned_reasons(self._ban_reasons)
        now = time.time()
        age_days = self.warmup_days_elapsed(key) if key in self._key_first_seen else None
        self._ban_meta[key] = {"banned_at": now, "key_age_days_at_ban": age_days}
        _save_ban_meta(self._ban_meta)
        self.keys = [k for k in self.keys if k != key]
        _save_banned_keys(self._banned)
        age_str = f", key was {age_days:.1f}d old" if age_days is not None else ""
        logger.error(f"[Gemini] Key {key[:12]}... permanently banned and removed from rotation ({len(self.keys)} keys remain){age_str}" + (f" — reason: {reason}" if reason else ""))
        try:
            from core import db_mark_gemini_key_banned
            asyncio.create_task(db_mark_gemini_key_banned(_gemini_key_hash(key), reason, int(now), age_days))
        except Exception as e:
            logger.warning(f"[Gemini] D1 ban-persist scheduling failed (non-fatal, /tmp file still authoritative until restart): {e}")

    def mark_healthy(self, key: str):
        self._cooldown_until.pop(key, None)
        _mark_gemini_key_healthy_today(key)

key_rotator = GeminiKeyRotator()

# Shared with app.py's qbm_extract_all_pages: each concurrent page-window
# slot sets this to a distinct offset before calling into any Gemini
# extraction path here, so ordered_keys(offset=...) below spreads
# concurrent Call1/Call2 requests across different starting keys instead
# of every slot racing for the same "healthiest" key and all 429-ing it
# at once (the bug behind "3 keys exhausted but Gemini still fails" --
# many concurrent slots all picked the SAME first-choice key before any
# of their mark_rate_limited() calls could land, so the exhaustion wasn't
# visible to the others in time). contextvars propagate through await
# chains automatically, so app.py setting this before calling into
# pdf_handler.py functions works with zero explicit parameter threading.
_qbm_key_offset_ctx = contextvars.ContextVar("_qbm_key_offset_ctx", default=0)

# Per-page account-diversity tracking: app.py's page-extraction loop resets
# this to a fresh empty set at the start of each page, then every Gemini
# call within that SAME page (Call1, heading-scan, Call2, missing-recovery
# retries, ...) appends the account it actually used. ordered_keys_
# avoiding_accounts() reads this so a page's later calls prefer a DIFFERENT
# account than its own earlier calls already used, instead of every call
# independently picking the same "healthiest" account and concentrating
# that one page's whole call sequence onto a single Google account. Falls
# back to normal healthiest-first ordering once every available account has
# already been used once this page (never blocks a call for lack of a
# fresh account).
_qbm_page_used_accounts_ctx = contextvars.ContextVar("_qbm_page_used_accounts_ctx", default=None)

# ============================================================
# OPENROUTER KEY ROTATION
# ============================================================
class OpenRouterKeyRotator:
    COOLDOWN_SECONDS = 60

    def __init__(self):
        self.keys = []
        self.current = 0
        self._cooldown_until = {}
        self._load_keys()

    def _load_keys(self):
        raw = os.environ.get("OPENROUTER_KEYS", "")
        if raw:
            self.keys = [k.strip() for k in raw.split(",") if k.strip()]
        logger.info(f"[OpenRouter] Loaded {len(self.keys)} keys")

    def get_key(self):
        if not self.keys:
            raise ValueError("No OpenRouter keys available")
        key = self.keys[self.current % len(self.keys)]
        self.current = (self.current + 1) % len(self.keys)
        return key

    def has_keys(self) -> bool:
        return len(self.keys) > 0

    def ordered_keys(self):
        now = time.time()
        healthy = [k for k in self.keys if self._cooldown_until.get(k, 0) <= now]
        cooling = [k for k in self.keys if self._cooldown_until.get(k, 0) > now]
        return healthy + cooling

    def mark_rate_limited(self, key: str):
        self._cooldown_until[key] = time.time() + self.COOLDOWN_SECONDS

    def mark_healthy(self, key: str):
        self._cooldown_until.pop(key, None)

openrouter_rotator = OpenRouterKeyRotator()

# ============================================================
# MCQ GENERATION PROMPTS
# ============================================================
MCQ_PROMPT_WITH_COUNT = """📝 এই page-টা থেকে MCQ বানাও।

🎯 MAIN RULE — MAXIMUM CONTENT USE: পুরো page-এর প্রতিটা অংশ (প্রতিটা প্যারাগ্রাফ, লাইন, বক্স/ছক, হাইলাইট/মার্ক করা অংশ) ভালোভাবে পড়ো এবং যত বেশি সম্ভব actual তথ্য থেকে MCQ বানাও — কোনো তথ্যবহুল অংশ বাদ দেওয়া যাবে না, পুরো page-এর content maximize করে ব্যবহার করবে।

- Page-এ আগে থেকে MCQ (question+options) থাকলে হুবহু (verbatim) extract করো। না থাকলে, তথ্য থেকে নতুন MCQ বানাও।
- হাইলাইট/মার্ক/আন্ডারলাইন করা লাইন থাকলে সেগুলো থেকে অবশ্যই MCQ বানাবে (সবার আগে, মিস করা যাবে না)।
- বক্স/ছক/সারণিতে তথ্য থাকলে প্রতিটা থেকে অন্তত ১টা MCQ বানাও।
- Question ও option-এর তথ্য অবশ্যই এই page-এর নিজের content থেকে আসবে — বাইরের knowledge দিয়ে বানানো যাবে না।
- টপিকের নাম/হেডলাইন/পেইজ নম্বরের মতো navigation/label টেক্সট থেকে MCQ বানাবে না।
- একই প্রশ্ন দুইবার (হুবহু বা ঘুরিয়ে) বানানো যাবে না।
- ভাষা: প্রতিটা MCQ (question, options, explanation) যে অংশ/লাইন/অনুচ্ছেদ থেকে বানানো হচ্ছে, ঠিক সেই অংশটা page-এ যে ভাষায় লেখা (বাংলা/ইংরেজি), MCQ-টাও ঠিক সেই ভাষাতেই লিখবে — কখনো translate করবে না। Page-এর কিছু অংশ বাংলা, কিছু অংশ ইংরেজি (mixed) হলে, প্রতিটা MCQ তার নিজের source-অংশের ভাষা অনুসরণ করবে (একই page-এ কিছু MCQ বাংলা, কিছু ইংরেজি — এটাই সঠিক, জোর করে একভাষায় আনা যাবে না)।
🔴🔴 ABSOLUTE CONTENT-LOCK (সর্বোচ্চ গুরুত্বপূর্ণ নিয়ম, ১০০০% মানতে হবে): question, প্রতিটা option, ব্যাখ্যা — সব কিছুর প্রতিটা word/fact/number/নাম শুধুমাত্র এই page-এ চোখে দেখা যাওয়া content থেকেই আসবে। নিজের জ্ঞান/training data/সাধারণ জ্ঞান থেকে এক ফোঁটাও তথ্য যোগ করা সম্পূর্ণ নিষিদ্ধ — বিষয়টা যতই সহজ/পরিচিত মনে হোক না কেন। কোনো option সম্পূর্ণ করতে page-এ নেই এমন কোনো তথ্য (এমনকি সঠিক তথ্য হলেও) বসাতে হলে, সেই MCQ-টাই সম্পূর্ণ বাদ দাও — কখনো নিজে থেকে বানিয়ে/অনুমান করে বসাবে না। প্রতিটা MCQ লেখার আগে নিজেকে যাচাই করো: "এই question ও প্রতিটা option-এর প্রতিটা শব্দ কি আমি এই page-এর ছবিতে হুবহু দেখতে পাচ্ছি?" — উত্তর "না" হলে সেই MCQ বাদ দাও।

💥প্রশ্ন: ছোট (১-২ লাইন)
💥অপশন: ৪টি, সবগুলোই factual, একটাই সঠিক উত্তর (হ্যাঁ/না/সত্য/মিথ্যা না)
💥উত্তর: A/B/C/D — সব প্রশ্নে একই letter না, ছড়িয়ে দাও
-MUST বানাতে হবে exactly {count} টি MCQ, কম বেশি নয়
💥ব্যাখ্যা (MAX 165 শব্দ): সঠিক উত্তর কেন সঠিক + বাকি ৩টা কেন ভুল, শুধু page content থেকে (বাইরের knowledge না), source-reference phrase ("টেক্সট অনুসারে" ইত্যাদি) ছাড়া সরাসরি fact আকারে।

Topic: {topic}
Page: {page}

MUST Return ONLY valid JSON array, no markdown:
[{{"question":"...","options":["option1","option2","option3","option4"],"answer":"B","explanation":"..."}}]"""

MCQ_PROMPT_MAX = """📝 এই page-টা থেকে MCQ বানাও।

🎯 MAIN RULE — MAXIMUM CONTENT USE: পুরো page-এর প্রতিটা অংশ (প্রতিটা প্যারাগ্রাফ, লাইন, বক্স/ছক, হাইলাইট/মার্ক করা অংশ) ভালোভাবে পড়ো এবং page-এ যত তথ্য আছে তার maximum ব্যবহার করে MCQ বানাও — কোনো তথ্যবহুল অংশ বাদ দেওয়া যাবে না।

- Page-এ আগে থেকে MCQ (question+options) থাকলে হুবহু (verbatim) extract করো। না থাকলে, তথ্য থেকে নতুন MCQ বানাও।
- হাইলাইট/মার্ক/আন্ডারলাইন করা লাইন থাকলে সেগুলো থেকে অবশ্যই MCQ বানাবে (সবার আগে, মিস করা যাবে না)।
- বক্স/ছক/সারণিতে তথ্য থাকলে প্রতিটা থেকে অন্তত ১টা MCQ বানাও, তথ্য বেশি থাকলে একাধিক MCQ বানাও।
- Question ও option-এর তথ্য অবশ্যই এই page-এর নিজের content থেকে আসবে — বাইরের knowledge দিয়ে বানানো যাবে না।
- টপিকের নাম/হেডলাইন/পেইজ নম্বরের মতো navigation/label টেক্সট থেকে MCQ বানাবে না।
- একই প্রশ্ন দুইবার (হুবহু বা ঘুরিয়ে) বানানো যাবে না; হাবিজাবি/মানহীন MCQ বানানো যাবে না।
- ভাষা: প্রতিটা MCQ (question, options, explanation) যে অংশ/লাইন/অনুচ্ছেদ থেকে বানানো হচ্ছে, ঠিক সেই অংশটা page-এ যে ভাষায় লেখা (বাংলা/ইংরেজি), MCQ-টাও ঠিক সেই ভাষাতেই লিখবে — কখনো translate করবে না। Page-এর কিছু অংশ বাংলা, কিছু অংশ ইংরেজি (mixed) হলে, প্রতিটা MCQ তার নিজের source-অংশের ভাষা অনুসরণ করবে (একই page-এ কিছু MCQ বাংলা, কিছু ইংরেজি — এটাই সঠিক, জোর করে একভাষায় আনা যাবে না)।
🔴🔴 ABSOLUTE CONTENT-LOCK (সর্বোচ্চ গুরুত্বপূর্ণ নিয়ম, ১০০০% মানতে হবে): question, প্রতিটা option, ব্যাখ্যা — সব কিছুর প্রতিটা word/fact/number/নাম শুধুমাত্র এই page-এ চোখে দেখা যাওয়া content থেকেই আসবে। নিজের জ্ঞান/training data/সাধারণ জ্ঞান থেকে এক ফোঁটাও তথ্য যোগ করা সম্পূর্ণ নিষিদ্ধ — বিষয়টা যতই সহজ/পরিচিত মনে হোক না কেন। কোনো option সম্পূর্ণ করতে page-এ নেই এমন কোনো তথ্য (এমনকি সঠিক তথ্য হলেও) বসাতে হলে, সেই MCQ-টাই সম্পূর্ণ বাদ দাও — কখনো নিজে থেকে বানিয়ে/অনুমান করে বসাবে না। প্রতিটা MCQ লেখার আগে নিজেকে যাচাই করো: "এই question ও প্রতিটা option-এর প্রতিটা শব্দ কি আমি এই page-এর ছবিতে হুবহু দেখতে পাচ্ছি?" — উত্তর "না" হলে সেই MCQ বাদ দাও।

📊 COUNT: default target কমপক্ষে ১৫টি MCQ (user নির্দিষ্ট সংখ্যা না দিলে) — page-এ তথ্য বেশি থাকলে ৩৫ পর্যন্ত যেতে পারো, ৬-১০টায় থেমে যাওয়া চলবে না যতক্ষণ page-এ আরও extract-যোগ্য তথ্য আছে। তথ্য সত্যিই কম থাকলে minimum 10, একদম sparse হলে minimum 5।

💥প্রশ্ন: ছোট (১-২ লাইন), সব ধরনের angle থেকে (direct fact, reverse, cause-effect, comparison, "কোনটি সঠিক নয়" ইত্যাদি মিক্স)
💥অপশন: ৪টি, সবগুলোই factual, একটাই সঠিক উত্তর (হ্যাঁ/না/সত্য/মিথ্যা না)
💥উত্তর: A/B/C/D — সব প্রশ্নে একই letter না, ছড়িয়ে দাও
💥ব্যাখ্যা (MAX 165 শব্দ): সঠিক উত্তর কেন সঠিক + বাকি ৩টা কেন ভুল, শুধু page content থেকে (বাইরের knowledge না), source-reference phrase ("টেক্সট অনুসারে" ইত্যাদি) ছাড়া সরাসরি fact আকারে।

Topic: {topic}
Page: {page}

MUST Return ONLY valid JSON array, no markdown:
[{{"question":"...","options":["option1","option2","option3","option4"],"answer":"C","explanation":"..."}}]"""


# ============================================================
# /pdfs: SINGLE-CALL TOPIC-DETECT + SEGMENT-LOCKED MCQ GENERATION
# ============================================================
# Per user requirement: exactly ONE Gemini call per page must do everything —
# detect every main/sub topic (with OCR+content-based self-verify), lock each
# topic's content boundary, AND generate that topic's MCQs strictly from its
# own content, all in one response. No image cropping (would risk losing
# content at crop edges) — the model reads the FULL page image once and
# self-organizes its own MCQ output by topic, tagging each MCQ with which
# topic/sub-topic it came from. This makes leaking one topic's content into
# another topic's MCQ set an internal-consistency failure the model itself
# must avoid while writing the single JSON response, and the code-level
# post-filter below (_pdfs_reconcile_mcq_topics) double-checks the tags
# against the model's own detected topic list before anything is used.
PDFS_TOPIC_DETECT_PROMPT = """📝 /pdfs Call1 — TOPIC DETECTION ONLY (এই ধাপে কোনো MCQ বানাবে না, শুধু topic/sub-topic identify করো)

পুরো page স্ক্যান করে সব MAIN TOPIC ও SUB TOPIC identify করো এই cue দিয়ে:
- MAIN TOPIC: content-এর উপরে/মাঝখানে (top-center), bold + অন্য টেক্সট থেকে বড় ফন্ট, প্রায়ই আলাদা background/box/boundary দিয়ে ঘেরা, আগে special marker/symbol থাকতে পারে।
- SUB TOPIC (optional): main topic-এর নিচে, ছোট ফন্ট, background হালকা ভিন্ন color হতে পারে (full-white না), আগে প্রায়ই colon (: বা ঃ), আগে marker/symbol থাকতে পারে।

প্রতিটা candidate-এর জন্য বাধ্যতামূলক VERIFY (সন্দেহ থাকুক বা না থাকুক সবসময়): heading OCR/visual read করে raw name বের করো, তারপর সেই heading-এর নিচের/আশেপাশের actual body content সম্পূর্ণ পড়ে বুঝে নাও content আসলে কী বিষয়ে। raw name আর content-এর প্রকৃত বিষয় না মিললে (বানান ভুল/OCR-misread/garbled), content বুঝে সঠিক নাম নিজে ঠিক করে দাও — content-ই আসল সত্য, heading-এর লেখা শুধু hint, blind copy কখনো না।

প্রতিটা confirmed topic-এর জন্য নির্ধারণ করো ঠিক কোন প্যারাগ্রাফ/লাইন/বক্স/সারণি তার নিজের content (content-boundary lock) — দুইটা topic-এর content কখনো overlap/split/duplicate করা যাবে না; overlap মনে হলে যে heading content-টার সবচেয়ে কাছে/উপরে সেটাই owner। এই boundary-র summary output-এ দাও যাতে Call2 exactly জানে কোন topic-এর content কোথা থেকে কোথায়।

কোনো clear topic/sub-topic না পেলে single virtual topic ধরো: main="{topic}", sub=null (পুরো page-ই তার content)।

Page: {page}

MUST Return ONLY valid JSON array, no markdown, no MCQs — শুধু detected topics:
[{{"main_topic":"...","sub_topic":"..." or null,"content_summary":"এই topic-এর content ঠিক কোথা থেকে কোথায়/কী নিয়ে, 1-2 line"}}]"""


PDFS_MCQ_GENERATE_PROMPT = """📝 /pdfs Call2 — MCQ GENERATION (topic detection Call1-এ আগেই হয়ে গেছে, এখানে শুধু MCQ বানাও)

Call1-এ এই page-এর জন্য এই topic/sub-topic গুলো ইতিমধ্যে confirm করা হয়েছে (content-boundary lock সহ):
{detected_topics}

প্রতিটা confirmed topic/sub-topic-এর জন্য আলাদা করে MCQ বানাও:
🔒 SOURCE-LOCK + TOPIC-LOCK (ABSOLUTE): প্রতিটা MCQ শুধুমাত্র তার নিজের topic-এর Call1-এ lock করা content-boundary থেকেই বানাবে। একটা topic-এর MCQ-তে অন্য topic-এর content/fact কখনো মিশতে পারবে না, এমনকি অন্য topic-এ সহজ/ভালো content থাকলেও। প্রতিটা MCQ output-এ অবশ্যই সেটা কোন main_topic ও sub_topic থেকে এসেছে সেটা সঠিকভাবে লিখতে হবে (Call1-এ ফাইনাল করা নাম অনুযায়ী, exact same spelling) — ভুল topic-এ MCQ tag করা কঠোরভাবে নিষিদ্ধ, কারণ এই ট্যাগ দিয়েই পরে output topic-wise ভাগ হবে।
🔴 STRICT PAGE-ONLY CONTENT (ABSOLUTE, প্রশ্ন+অপশন+ব্যাখ্যা সবক্ষেত্রে): question, প্রতিটা option, ও ব্যাখ্যা — সব শুধুমাত্র এই page-এ visible content থেকে আসবে। বাইরের সাধারণ জ্ঞান/training data থেকে কোনো fact, number, নাম, তারিখ, বা detail যোগ করা কঠোরভাবে নিষিদ্ধ — এমনকি সেটা সত্যি হলেও এবং topic-টা পরিচিত মনে হলেও। কোনো option-এর মধ্যে যদি page-এ না থাকা কোনো তথ্য বসাতে হয়, সেই MCQ-টাই বাদ দাও, বানিয়ে option দিও না। প্রতিটা MCQ লেখার আগে নিজেকে verify করো: "এই question/option-এর প্রতিটা শব্দ কি আমি এই page-এর ছবিতে সরাসরি দেখতে পাচ্ছি?" — না পারলে সেটা রাখা যাবে না।
-🔒 TWO-MODE RULE: page-এ আগে থেকেই MCQ (question+options) থাকলে সেগুলো 100% VERBATIM extract করবে, নইলে content থেকে নতুন MCQ বানাবে।
-🔒 NO-DUPLICATE-FROM-EXISTING-MCQ: existing question rephrase করে নতুন MCQ বানানো নিষিদ্ধ। NO-MCQ-FROM-ANSWER/EXPLANATION-TEXT: কোনো প্রশ্নের answer/explanation paragraph থেকে সরাসরি নতুন MCQ বানানো নিষিদ্ধ, শুধু actual info-content থেকেই বানাবে।
-🔴 HIGHLIGHT/MARK PRIORITY (ABSOLUTE FIRST): হাইলাইটেড/মার্ক করা/আন্ডারলাইন করা লাইন থেকে MCQ সবার আগে বানাবে (মিস করা যাবে না), তারপর বাকি normal content থেকে।
-প্রতিটা topic থেকে গড়ে {per_topic_count} টি MCQ target করো (topic-এ content বেশি/কম থাকলে স্বাভাবিকভাবে কমবেশি হতে পারে, কিন্তু কোনো topic 0 রাখা যাবে না যদি তার নিজের content থাকে)।
-টপিকের নাম/অধ্যায়ের নাম/হেডলাইন/পেইজ সংখ্যা/navigation label থেকে MCQ বানাবে না।
-প্রতিটা অপশন actual factual content হতে হবে, হ্যাঁ/না/সত্য/মিথ্যা না।
💥প্রশ্ন: ছোট (১/১.৫/২ লাইন)
💥অপশন: ৪টি, সঠিক উত্তর একটিই
💥উত্তর: A/B/C/D — বিভিন্ন position-এ ছড়িয়ে দিবে, সব একই letter না।
🔒 ANSWER RELEVANCY SANITY CHECK: page-এ answer আগে থেকে marked থাকলে, সেটা question+options-এর সাথে logically সঠিক কিনা re-check করো; স্পষ্ট mismatch হলেই শুধু নিজের জ্ঞান দিয়ে override করবে।
💥ব্যাখ্যা (MAX 165 WORDS মূল অংশ): সঠিক উত্তর কেন সঠিক + বাকি ৩টা কেন ভুল, সব মিলিয়ে ১৬৫ শব্দের মধ্যে; না আঁটলে extra detail নিচে আলাদা লাইনে, মূল অংশ কখনো truncate না। শুধু page content থেকে, বাইরের knowledge না। source-reference phrase ("টেক্সট অনুসারে" ইত্যাদি) নিষিদ্ধ।

🌐 LANGUAGE RULE: source-এর ভাষায় (বাংলা হলে বাংলা, ইংরেজি হলে ইংরেজি — translate করবে না)।

Page: {page}

MUST Return ONLY valid JSON array, no markdown, EVERY item MUST include main_topic + sub_topic (Call1-এ detected topic list থেকে exact নাম মিলিয়ে):
[{{"main_topic":"...","sub_topic":"..." or null,"question":"...","options":["option1","option2","option3","option4"],"answer":"B","explanation":"..."}}]"""


# Kept for reference/backward-compat only -- superseded by the Call1
# (PDFS_TOPIC_DETECT_PROMPT) + Call2 (PDFS_MCQ_GENERATE_PROMPT) split
# above (2026-08-24, per request: /pdfs now mirrors /topic's separate
# detect-then-generate call structure instead of doing both in one call).
PDFS_TOPIC_MCQ_PROMPT = """📝 Special MCQ TYPE: /pdfs Topic-wise Generation (SINGLE CALL — topic detection + MCQ generation together)

🎯 এই কাজটা ২টা ধাপে করবে, কিন্তু একটাই response-এ:

═══ ধাপ A: TOPIC DETECTION + SELF-VERIFY (MCQ বানানোর আগে বাধ্যতামূলক) ═══
পুরো page স্ক্যান করে সব MAIN TOPIC ও SUB TOPIC identify করো এই cue দিয়ে:
- MAIN TOPIC: content-এর উপরে/মাঝখানে (top-center), bold + অন্য টেক্সট থেকে বড় ফন্ট, প্রায়ই আলাদা background/box/boundary দিয়ে ঘেরা, আগে special marker/symbol থাকতে পারে।
- SUB TOPIC (optional): main topic-এর নিচে, ছোট ফন্ট, background হালকা ভিন্ন color হতে পারে (full-white না), আগে প্রায়ই colon (: বা ঃ), আগে marker/symbol থাকতে পারে।

প্রতিটা candidate-এর জন্য বাধ্যতামূলক VERIFY (সন্দেহ থাকুক বা না থাকুক সবসময়): heading OCR/visual read করে raw name বের করো, তারপর সেই heading-এর নিচের/আশেপাশের actual body content সম্পূর্ণ পড়ে বুঝে নাও content আসলে কী বিষয়ে। raw name আর content-এর প্রকৃত বিষয় না মিললে (বানান ভুল/OCR-misread/garbled), content বুঝে সঠিক নাম নিজে ঠিক করে দাও — content-ই আসল সত্য, heading-এর লেখা শুধু hint, blind copy কখনো না।

প্রতিটা confirmed topic-এর জন্য নির্ধারণ করো ঠিক কোন প্যারাগ্রাফ/লাইন/বক্স/সারণি তার নিজের content (content-boundary lock) — দুইটা topic-এর content কখনো overlap/split/duplicate করা যাবে না; overlap মনে হলে যে heading content-টার সবচেয়ে কাছে/উপরে সেটাই owner।

কোনো clear topic/sub-topic না পেলে single virtual topic ধরো: main="{topic}", sub=null (পুরো page-ই তার content)।

═══ ধাপ B: প্রতিটা confirmed topic/sub-topic-এর জন্য আলাদা করে MCQ বানাও ═══
🔒 SOURCE-LOCK + TOPIC-LOCK (ABSOLUTE): প্রতিটা MCQ শুধুমাত্র তার নিজের topic-এর ধাপ-A-তে lock করা content-boundary থেকেই বানাবে। একটা topic-এর MCQ-তে অন্য topic-এর content/fact কখনো মিশতে পারবে না, এমনকি অন্য topic-এ সহজ/ভালো content থাকলেও। প্রতিটা MCQ output-এ অবশ্যই সেটা কোন main_topic ও sub_topic থেকে এসেছে সেটা সঠিকভাবে লিখতে হবে (ধাপ A-তে ফাইনাল করা নাম অনুযায়ী, exact same spelling) — ভুল topic-এ MCQ tag করা কঠোরভাবে নিষিদ্ধ, কারণ এই ট্যাগ দিয়েই পরে output topic-wise ভাগ হবে।
🔴 STRICT PAGE-ONLY CONTENT (ABSOLUTE, প্রশ্ন+অপশন+ব্যাখ্যা সবক্ষেত্রে): question, প্রতিটা option, ও ব্যাখ্যা — সব শুধুমাত্র এই page-এ visible content থেকে আসবে। বাইরের সাধারণ জ্ঞান/training data থেকে কোনো fact, number, নাম, তারিখ, বা detail যোগ করা কঠোরভাবে নিষিদ্ধ — এমনকি সেটা সত্যি হলেও এবং topic-টা পরিচিত মনে হলেও। কোনো option-এর মধ্যে যদি page-এ না থাকা কোনো তথ্য বসাতে হয়, সেই MCQ-টাই বাদ দাও, বানিয়ে option দিও না। প্রতিটা MCQ লেখার আগে নিজেকে verify করো: "এই question/option-এর প্রতিটা শব্দ কি আমি এই page-এর ছবিতে সরাসরি দেখতে পাচ্ছি?" — না পারলে সেটা রাখা যাবে না।
-🔒 TWO-MODE RULE: page-এ আগে থেকেই MCQ (question+options) থাকলে সেগুলো 100% VERBATIM extract করবে, নইলে content থেকে নতুন MCQ বানাবে।
-🔒 NO-DUPLICATE-FROM-EXISTING-MCQ: existing question rephrase করে নতুন MCQ বানানো নিষিদ্ধ। NO-MCQ-FROM-ANSWER/EXPLANATION-TEXT: কোনো প্রশ্নের answer/explanation paragraph থেকে সরাসরি নতুন MCQ বানানো নিষিদ্ধ, শুধু actual info-content থেকেই বানাবে।
-🔴 HIGHLIGHT/MARK PRIORITY (ABSOLUTE FIRST): হাইলাইটেড/মার্ক করা/আন্ডারলাইন করা লাইন থেকে MCQ সবার আগে বানাবে (মিস করা যাবে না), তারপর বাকি normal content থেকে।
🔴🔴 EXHAUSTIVE COVERAGE (MANDATORY): {per_topic_count} শুধু ন্যূনতম টার্গেট, ceiling না — প্রতিটা topic-এর content-এ যত fact/line/data থেকে valid MCQ বানানো সম্ভব সবগুলো থেকেই বানাও, content বেশি থাকলে সংখ্যাও বেশি হবে। মাত্র ৩-৪টা বানিয়ে থেমে যাওয়া ভুল যদি সেই topic-এ আরও extract-যোগ্য content বাকি থাকে।
-প্রতিটা topic থেকে ন্যূনতম {per_topic_count} টি MCQ বানাও (topic-এ content বেশি থাকলে আরও বেশি বানাও, কিন্তু কোনো topic 0 রাখা যাবে না যদি তার নিজের content থাকে)।
-টপিকের নাম/অধ্যায়ের নাম/হেডলাইন/পেইজ সংখ্যা/navigation label থেকে MCQ বানাবে না।
-প্রতিটা অপশন actual factual content হতে হবে, হ্যাঁ/না/সত্য/মিথ্যা না।
💥প্রশ্ন: ছোট (১/১.৫/২ লাইন)
💥অপশন: ৪টি, সঠিক উত্তর একটিই
💥উত্তর: A/B/C/D — বিভিন্ন position-এ ছড়িয়ে দিবে, সব একই letter না।
🔒 ANSWER RELEVANCY SANITY CHECK: page-এ answer আগে থেকে marked থাকলে, সেটা question+options-এর সাথে logically সঠিক কিনা re-check করো; স্পষ্ট mismatch হলেই শুধু নিজের জ্ঞান দিয়ে override করবে।
💥ব্যাখ্যা (MAX 165 WORDS মূল অংশ): সঠিক উত্তর কেন সঠিক + বাকি ৩টা কেন ভুল, সব মিলিয়ে ১৬৫ শব্দের মধ্যে; না আঁটলে extra detail নিচে আলাদা লাইনে, মূল অংশ কখনো truncate না। শুধু page content থেকে, বাইরের knowledge না। source-reference phrase ("টেক্সট অনুসারে" ইত্যাদি) নিষিদ্ধ।

🌐 LANGUAGE RULE: প্রতিটা MCQ তার নিজের source content যে ভাষায় (বাংলা/ইংরেজি) লেখা, ঠিক সেই ভাষাতেই লিখবে — translate করবে না। Page mixed-language হলে (কিছু অংশ বাংলা, কিছু ইংরেজি), প্রতিটা MCQ তার উৎস-অংশের ভাষা অনুসরণ করবে, একই page-এ কিছু MCQ বাংলা কিছু ইংরেজি হওয়া স্বাভাবিক ও সঠিক।
🔴🔴 ABSOLUTE CONTENT-LOCK (১০০০% মানতে হবে): question/option/ব্যাখ্যার প্রতিটা শব্দ শুধু page-এ চোখে দেখা content থেকেই আসবে — নিজের জ্ঞান/training data থেকে বিন্দুমাত্র তথ্য যোগ করা নিষিদ্ধ, যতই সহজ/পরিচিত বিষয় মনে হোক। Page-এ নেই এমন কোনো তথ্য option-এ বসাতে হলে সেই MCQ-ই বাদ দাও।

Page: {page}

MUST Return ONLY valid JSON array, no markdown, EVERY item MUST include main_topic + sub_topic:
[{{"main_topic":"...","sub_topic":"..." or null,"question":"...","options":["option1","option2","option3","option4"],"answer":"B","explanation":"..."}}]"""


PDFS_CALL2_MCQ_ONLY_PROMPT = """📝 /pdfs Call2 — MCQ GENERATION ONLY (topic detection already done in Call1, DO NOT re-detect)

🔒 CONFIRMED TOPICS FOR THIS PAGE (from Call1, already verified — use these EXACT names, do not rename/reinterpret):
{topics_list}

═══ প্রতিটা confirmed topic/sub-topic-এর জন্য আলাদা করে MCQ বানাও ═══
🔒 SOURCE-LOCK + TOPIC-LOCK (ABSOLUTE): প্রতিটা MCQ শুধুমাত্র তার নিজের topic-এর content-boundary থেকেই বানাবে (উপরের list-এর যে heading-এর নিচে/কাছে সেই content, সেটাই তার topic)। একটা topic-এর MCQ-তে অন্য topic-এর content/fact কখনো মিশতে পারবে না। প্রতিটা MCQ output-এ অবশ্যই সেটা কোন main_topic (উপরের list থেকে exact same spelling) থেকে এসেছে সেটা লিখতে হবে — যদি page-এ উপরের কোনো topic-এর সাথে না মেলে এমন content থাকে, সেটাকে সবচেয়ে কাছের/প্রাসঙ্গিক topic-এর আন্ডারে রাখো।
🔴 STRICT PAGE-ONLY CONTENT (ABSOLUTE, প্রশ্ন+অপশন+ব্যাখ্যা সবক্ষেত্রে): question, প্রতিটা option, ও ব্যাখ্যা — সব শুধুমাত্র এই page-এ visible content থেকে আসবে। বাইরের সাধারণ জ্ঞান/training data থেকে কোনো fact যোগ করা কঠোরভাবে নিষিদ্ধ। কোনো option-এর মধ্যে page-এ না থাকা তথ্য বসাতে হলে সেই MCQ-টাই বাদ দাও।
-🔒 TWO-MODE RULE: page-এ আগে থেকেই MCQ থাকলে 100% VERBATIM extract করবে, নইলে content থেকে নতুন MCQ বানাবে।
-🔒 NO-DUPLICATE-FROM-EXISTING-MCQ, NO-MCQ-FROM-ANSWER/EXPLANATION-TEXT.
-🔴 HIGHLIGHT/MARK PRIORITY (ABSOLUTE FIRST): হাইলাইটেড/মার্ক করা লাইন থেকে MCQ সবার আগে বানাবে।
🔴🔴 EXHAUSTIVE COVERAGE (MANDATORY, NOT OPTIONAL): এই page-এ যত fact/definition/data/line আছে যেখান থেকে একটা valid MCQ বানানো সম্ভব — প্রতিটা থেকে MCQ বানাতে হবে। {per_topic_count} শুধু একটা TARGET/ন্যূনতম সংখ্যা, ceiling না — page-এ যদি তার চেয়ে বেশি বানানোর মতো content থাকে (বেশি বাক্য/fact/উপ-পয়েন্ট), তাহলে বেশি MCQ-ই বানাতে হবে। শুধু ৩-৪টা বানিয়ে থেমে যাওয়া ভুল, যদি page-এ আরও extractable content অবশিষ্ট থাকে। থামার আগে নিজেকে যাচাই করো: "এই page-এর প্রতিটা লাইন/fact কি আমি কভার করেছি?" — না করলে আরও MCQ যোগ করো।
-প্রতিটা topic থেকে ন্যূনতম {per_topic_count} টি MCQ বানাও (content বেশি থাকলে আরও বেশি), কোনো topic 0 রাখা যাবে না যদি তার নিজের content থাকে।
-টপিকের নাম/হেডলাইন/পেইজ সংখ্যা থেকে MCQ বানাবে না। প্রতিটা অপশন actual factual content, হ্যাঁ/না না।
💥প্রশ্ন: ছোট (১/১.৫/২ লাইন) | 💥অপশন: ৪টি, সঠিক উত্তর একটিই | 💥উত্তর: A/B/C/D ছড়িয়ে দিবে।
💥ব্যাখ্যা (MAX 165 WORDS): সঠিক উত্তর কেন সঠিক + বাকি ৩টা কেন ভুল, শুধু page content থেকে।

🌐 LANGUAGE RULE: প্রতিটা MCQ তার নিজের source content যে ভাষায় (বাংলা/ইংরেজি) লেখা, ঠিক সেই ভাষাতেই লিখবে — translate করবে না। Page mixed-language হলে (কিছু অংশ বাংলা, কিছু ইংরেজি), প্রতিটা MCQ তার উৎস-অংশের ভাষা অনুসরণ করবে, একই page-এ কিছু MCQ বাংলা কিছু ইংরেজি হওয়া স্বাভাবিক ও সঠিক।
🔴🔴 ABSOLUTE CONTENT-LOCK (১০০০% মানতে হবে): question/option/ব্যাখ্যার প্রতিটা শব্দ শুধু page-এ চোখে দেখা content থেকেই আসবে — নিজের জ্ঞান/training data থেকে বিন্দুমাত্র তথ্য যোগ করা নিষিদ্ধ, যতই সহজ/পরিচিত বিষয় মনে হোক। Page-এ নেই এমন কোনো তথ্য option-এ বসাতে হলে সেই MCQ-ই বাদ দাও।

Page: {page}

MUST Return ONLY valid JSON array, EVERY item MUST include main_topic + sub_topic:
[{{"main_topic":"...","sub_topic":"..." or null,"question":"...","options":["option1","option2","option3","option4"],"answer":"B","explanation":"..."}}]"""


def _pdfs_reconcile_mcq_topics(mcqs: list, fallback: str, allowed_topics: list = None) -> list:
    """Code-level backstop (not prompt-only) — runs on every /pdfs generation
    result before it's used anywhere else. Ensures every MCQ has a clean,
    non-empty main_topic (never silently dropped into the wrong bucket by a
    blank/garbled tag), normalizes sub_topic (blank/whitespace -> None), and
    strips the internal main_topic/sub_topic keys into the standard
    _pdfs_topic/_pdfs_subtopic keys app.py's grouping code expects — so a
    single point of truth decides the final topic bucket for every MCQ,
    instead of trusting the raw model output directly.

    allowed_topics (Call2 only): the exact main_topic names Call1 already
    confirmed for this page. If the model tags an MCQ with something NOT in
    this list (hallucinated/garbled topic name), force it onto the single
    allowed topic (if only one) or the page's fallback topic instead of
    creating a stray bucket that never appeared in Call1 -- this is what
    actually prevents one topic's MCQs from silently leaking into a wrong
    bucket."""
    out = []
    _allowed_norm = {t.strip().lower(): t.strip() for t in (allowed_topics or []) if t and t.strip()}
    for m in (mcqs or []):
        if not isinstance(m, dict) or not m.get("question"):
            continue
        main_t = (m.get("main_topic") or "").strip()
        if not main_t:
            main_t = fallback
        if _allowed_norm and main_t.strip().lower() not in _allowed_norm:
            main_t = next(iter(_allowed_norm.values())) if len(_allowed_norm) == 1 else fallback
        else:
            main_t = _allowed_norm.get(main_t.strip().lower(), main_t)
        sub_t = m.get("sub_topic")
        sub_t = sub_t.strip() if isinstance(sub_t, str) and sub_t.strip() else None
        m["_pdfs_topic"] = main_t[:60]
        m["_pdfs_subtopic"] = sub_t[:60] if sub_t else None
        m.pop("main_topic", None)
        m.pop("sub_topic", None)
        out.append(m)
    return out


async def _pdfs_gemini_call_with_retry(prompt: str, img: Image.Image, log_tag: str) -> str:
    """Shared Gemini-call-with-key-rotation-retry logic used by both /pdfs
    Call1 (topic detect) and Call2 (MCQ generate) -- extracted out of the
    old single-call generate_pdfs_topic_mcqs so both calls get the same
    key-rotation, daily-exhaustion skip, and timeout/retry behavior
    without duplicating it. Returns raw response text, or "" if every key
    failed (caller decides what empty means for its own step)."""
    _ordered = key_rotator.ordered_keys(offset=_qbm_key_offset_ctx.get())
    _all_marked_exhausted = bool(key_rotator.keys) and all(_is_gemini_key_exhausted_today(k) for k in key_rotator.keys)
    _live = [k for k in _ordered if not _is_gemini_key_exhausted_today(k)]
    if _live:
        _ordered = _live
    elif _all_marked_exhausted:
        # Don't blind-trust "every key exhausted" -- implausible for 40+
        # independent keys at once, likely a bad flag. Verify with 2 real
        # attempts before giving up to Groq/other fallbacks.
        logger.warning(f"[{log_tag}] all {len(key_rotator.keys)} Gemini keys marked daily-exhausted (suspicious) — retrying 2 keys for real before giving up")
        _ordered = _ordered[:2] if _ordered else []
    # User instruction (2026-08-25): try every live key before Groq -- only
    # stop early on a genuine backend/network outage (3 consecutive
    # non-quota failures), never just because a key-count ceiling was hit.
    max_retries = len(_ordered) if _ordered else 5
    _consecutive_infra_fails = 0
    _tried_keys = set()
    for attempt in range(max_retries):
        # Re-derive fresh healthy order each attempt instead of indexing a
        # stale pre-loop snapshot, so a key cooled/banned earlier in THIS
        # loop is never revisited while an untried healthy key exists.
        _fresh = key_rotator.ordered_keys(offset=_qbm_key_offset_ctx.get())
        _fresh = [k for k in _fresh if not _is_gemini_key_exhausted_today(k)] or _fresh
        _untried = [k for k in _fresh if k not in _tried_keys]
        if _untried:
            key = _untried[0]
        elif _fresh:
            key = _fresh[attempt % len(_fresh)]
        else:
            key = _ordered[attempt % len(_ordered)] if _ordered else key_rotator.get_key()
        _tried_keys.add(key)
        key_rotator.record_call(key)
        try:
            from google import genai as gai
            from google.genai import types
            client = gai.Client(
                api_key=key,
                http_options=types.HttpOptions(timeout=38000)
            )
            img_b64 = image_to_base64(img)

            def _call():
                return client.models.generate_content(
                    model="gemini-3.5-flash",
                    contents=[
                        types.Part.from_text(text=prompt),
                        types.Part.from_bytes(data=base64.b64decode(img_b64), mime_type="image/jpeg")
                    ],
                    config=types.GenerateContentConfig(max_output_tokens=8192)
                )
            _attempt_timeout = 40 if attempt == 0 else 25
            async with key_rotator.throttled_call(key=key):
                response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=_attempt_timeout)
            key_rotator.mark_healthy(key)
            return response.text or ""
        except Exception as e:
            err_str = _gemini_full_error_text(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                is_daily = "PerDay" in err_str or "generate_content_free_tier_requests" in err_str
                key_rotator.mark_rate_limited(key, daily_exhausted=is_daily)
                _consecutive_infra_fails = 0
            elif ("SUSPENDED" in err_str.upper() or "API_KEY_INVALID" in err_str.upper()
                  or "UNAUTHENTICATED" in err_str.upper() or "ACCOUNT_STATE_INVALID" in err_str.upper()
                  or "401" in err_str):
                key_rotator.mark_banned(key, reason=err_str[:200]); key_rotator.record_account_error(key)
                _consecutive_infra_fails = 0
            else:
                logger.warning(f"[{log_tag}] Attempt {attempt+1} failed: {type(e).__name__}: {err_str}")
                _consecutive_infra_fails += 1
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
            continue
    logger.warning(f"[{log_tag}] All keys failed — returning empty (caller will try Groq/other fallbacks)")
    return ""


async def _pdfs_call1_detect_topics(img: Image.Image, topic: str, page: int) -> list:
    """/pdfs Call1 — topic/sub-topic detection ONLY, no MCQs. Returns a list
    of {"main_topic":..., "sub_topic":..., "content_summary":...} dicts.
    Falls back to a single virtual topic (the page's given `topic` name)
    if detection fails/returns nothing, so Call2 always has something to
    work with rather than failing the whole page."""
    prompt = PDFS_TOPIC_DETECT_PROMPT.format(topic=topic, page=str(page).zfill(2))
    txt = await _pdfs_gemini_call_with_retry(prompt, img, "PDFS Call1")
    if not txt:
        return [{"main_topic": topic, "sub_topic": None, "content_summary": ""}]
    try:
        detected = _parse_mcq_json(txt)  # reuse the same tolerant JSON-array parser
    except Exception as e:
        logger.warning(f"[PDFS Call1] page {page}: parse failed: {e}")
        detected = []
    if not detected:
        return [{"main_topic": topic, "sub_topic": None, "content_summary": ""}]
    out = []
    for d in detected:
        if not isinstance(d, dict):
            continue
        main_t = (d.get("main_topic") or "").strip() or topic
        sub_t = d.get("sub_topic")
        sub_t = sub_t.strip() if isinstance(sub_t, str) and sub_t.strip() else None
        out.append({"main_topic": main_t[:60], "sub_topic": sub_t[:60] if sub_t else None,
                     "content_summary": (d.get("content_summary") or "")[:200]})
    logger.info(f"[PDFS Call1] page {page}: {len(out)} topic(s) detected")
    return out or [{"main_topic": topic, "sub_topic": None, "content_summary": ""}]


async def _pdfs_call2_generate_mcqs(img: Image.Image, detected_topics: list, page: int, mcq_count_hint: int, fallback_topic: str) -> list:
    """/pdfs Call2 — MCQ generation using the topics Call1 already
    detected. Returns MCQs already tagged with _pdfs_topic/_pdfs_subtopic
    via _pdfs_reconcile_mcq_topics, same contract as before."""
    topics_json = json.dumps(
        [{"main_topic": t["main_topic"], "sub_topic": t["sub_topic"]} for t in detected_topics],
        ensure_ascii=False
    )
    prompt = PDFS_MCQ_GENERATE_PROMPT.format(
        detected_topics=topics_json, page=str(page).zfill(2), per_topic_count=mcq_count_hint
    )
    txt = await _pdfs_gemini_call_with_retry(prompt, img, "PDFS Call2")
    if not txt:
        return []
    valid = _parse_mcq_json(txt)
    if not valid:
        logger.warning(f"[PDFS Call2] page {page}: 0 valid MCQs parsed — likely malformed/truncated JSON")
        return []
    valid = _pdfs_reconcile_mcq_topics(valid, fallback_topic)
    logger.info(f"[PDFS Call2] page {page}: {len(valid)} MCQs across "
                f"{len(set(m['_pdfs_topic'] for m in valid))} topic(s)")
    return valid


async def generate_pdfs_topic_mcqs(img: Image.Image, topic: str, page: int, mcq_count_hint: int = 15) -> list:
    """/pdfs TWO-CALL pipeline (2026-08-24, per request — mirrors /topic's
    separate detect-then-generate structure instead of doing both in one
    call): Call1 (_pdfs_call1_detect_topics) detects topic/sub-topic
    boundaries ONLY, no MCQs; Call2 (_pdfs_call2_generate_mcqs) then
    generates MCQs using exactly those detected topics. Same external
    signature/return shape as before (flat list of MCQ dicts, each
    carrying _pdfs_topic/_pdfs_subtopic) so app.py's caller needs zero
    changes -- this function is now a 2-call orchestrator instead of a
    single call."""
    detected_topics = await _pdfs_call1_detect_topics(img, topic, page)
    return await _pdfs_call2_generate_mcqs(img, detected_topics, page, mcq_count_hint, topic)


async def generate_pdfs_call2_mcqs(img: Image.Image, headings: list, topic: str, page: int,
                                    mcq_count_hint: int = 15, timing: dict = None, chat_id: int = None) -> tuple:
    """/pdfs Call2 (generation-only): headings is Call1's already-confirmed
    list of [{"main":..., "sub":...}] for this page — this function does
    NOT re-detect topics, it just generates MCQs and tags each with one of
    the given topic names (see PDFS_CALL2_MCQ_ONLY_PROMPT). Splitting
    detection (Call1) and generation (Call2) into separate calls means
    topic-detect never depends on generation succeeding and vice versa —
    same two-call shape as /topic and /bio use elsewhere in this file.
    Returns (mcqs, elapsed_seconds, model_used) so the caller can show
    per-page timing + model in the live dashboard. If timing dict is
    passed, also records timing['start']/['end'] for external use."""
    import time as _time
    _t0 = _time.time()
    _headings_list = headings or [{"main": topic, "sub": None}]
    _allowed_topics = [h.get("main") for h in _headings_list if h.get("main")]
    topics_list = "\n".join(
        f"- main: \"{h.get('main')}\"" + (f", sub: \"{h.get('sub')}\"" if h.get('sub') else "")
        for h in _headings_list
    )
    prompt = PDFS_CALL2_MCQ_ONLY_PROMPT.format(
        topics_list=topics_list, page=str(page).zfill(2), per_topic_count=mcq_count_hint)
    _ordered = key_rotator.ordered_keys(offset=_qbm_key_offset_ctx.get())
    _all_marked_exhausted = bool(key_rotator.keys) and all(_is_gemini_key_exhausted_today(k) for k in key_rotator.keys)
    _live = [k for k in _ordered if not _is_gemini_key_exhausted_today(k)]
    if _live:
        _ordered = _live
    elif _all_marked_exhausted:
        # Don't blind-trust an in-memory/D1 flag that says EVERY key is
        # daily-exhausted -- 40+ independent keys genuinely hitting quota
        # at the exact same moment is implausible, so this usually means
        # the flag was set wrongly somewhere. Verify with 2 real attempts
        # before giving up, instead of returning empty immediately.
        logger.warning(f"[PDFS-C2] all {len(key_rotator.keys)} Gemini keys marked daily-exhausted (suspicious) — retrying 2 keys for real before giving up")
        _ordered = _ordered[:2] if _ordered else []
    max_retries = len(_ordered) if _ordered else 5
    _consecutive_infra_fails = 0
    _tried_keys = set()
    for attempt in range(max_retries):
        _fresh = key_rotator.ordered_keys(offset=_qbm_key_offset_ctx.get())
        _fresh = [k for k in _fresh if not _is_gemini_key_exhausted_today(k)] or _fresh
        _untried = [k for k in _fresh if k not in _tried_keys]
        if _untried:
            key = _untried[0]
        elif _fresh:
            key = _fresh[attempt % len(_fresh)]
        else:
            key = _ordered[attempt % len(_ordered)] if _ordered else key_rotator.get_key()
        _tried_keys.add(key)
        key_rotator.record_call(key)
        try:
            from google import genai as gai
            from google.genai import types
            client = gai.Client(
                api_key=key,
                http_options=types.HttpOptions(timeout=38000)
            )
            img_b64 = image_to_base64(img)

            def _call():
                return client.models.generate_content(
                    model="gemini-3.5-flash",
                    contents=[
                        types.Part.from_text(text=prompt),
                        types.Part.from_bytes(data=base64.b64decode(img_b64), mime_type="image/jpeg")
                    ],
                    config=types.GenerateContentConfig(max_output_tokens=8192)
                )
            _attempt_timeout = 40 if attempt == 0 else 25
            async with key_rotator.throttled_call(key=key):
                response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=_attempt_timeout)
            valid = _parse_mcq_json(response.text)
            elapsed = round(_time.time() - _t0, 1)
            if not valid:
                logger.warning(f"[PDFS-C2] Page {page}: 0 valid MCQs parsed (attempt {attempt+1})")
            else:
                valid = _pdfs_reconcile_mcq_topics(valid, topic, allowed_topics=_allowed_topics)
            key_rotator.mark_healthy(key)
            try:
                import app as _app_mod
                _cid = chat_id if chat_id is not None else _app_mod._current_job_chat_id_ctx.get()
                if _cid is None:
                    logger.warning(f"[PDFS-C2] AI-call bump skipped: no chat_id available (page {page}) -- dashboard AI-call count will under-report")
                else:
                    _app_mod._bump_ai_call_count(_cid, model="Gemini")
            except Exception as _bump_e:
                logger.warning(f"[PDFS-C2] AI-call bump failed (page {page}): {type(_bump_e).__name__}: {_bump_e}")
            logger.info(f"[PDFS-C2] Page {page}: {len(valid)} MCQs in {elapsed}s (attempt {attempt+1}, gemini-3.5-flash)")
            return valid, elapsed, "Gemini" if valid else None
        except Exception as e:
            err_str = _gemini_full_error_text(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                is_daily = "PerDay" in err_str or "generate_content_free_tier_requests" in err_str
                key_rotator.mark_rate_limited(key, daily_exhausted=is_daily)
                _consecutive_infra_fails = 0
            elif ("SUSPENDED" in err_str.upper() or "API_KEY_INVALID" in err_str.upper()
                  or "UNAUTHENTICATED" in err_str.upper() or "ACCOUNT_STATE_INVALID" in err_str.upper()
                  or "401" in err_str):
                key_rotator.mark_banned(key, reason=err_str[:200]); key_rotator.record_account_error(key)
                _consecutive_infra_fails = 0
            else:
                logger.warning(f"[PDFS-C2] Attempt {attempt+1} failed: {type(e).__name__}: {err_str}")
                _consecutive_infra_fails += 1
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
            continue
    elapsed = round(_time.time() - _t0, 1)
    logger.warning(f"[PDFS-C2] All keys failed for page {page} after {elapsed}s")
    return [], elapsed, None


# ============================================================
# PDF TO IMAGES
# ============================================================
# v-RAM-fix: pdf2image (poppler) rendering was the biggest RAM spike risk on
# the old 512MB Render instance -- a large PDF at dpi=150 could use 100-300MB
# during conversion, so concurrency was capped hard at 1 and pages/call at 10.
# Now on 16GB HF Space there's ample headroom, so both are raised substantially
# while still keeping some ceiling as a sanity guard against runaway usage.
import threading as _threading
_PDF_CONVERT_LOCK = _threading.Semaphore(6)
_PDF_MAX_PAGES_PER_CALL = 60


def pdf_to_images(pdf_bytes: bytes, page_range: str = None) -> list:
    # Bounded wait (5 min) instead of indefinite block -- avoids thread-pool
    # exhaustion if many uploads queue up at once; caller gets a clear error
    # instead of the request hanging forever.
    if not _PDF_CONVERT_LOCK.acquire(timeout=300):
        raise RuntimeError("PDF conversion queue busy -- try again in a moment")
    try:
        from pdf2image import convert_from_bytes
        def _convert_batch(first: int, last: int):
            """Convert a whole page range in ONE convert_from_bytes call
            (single poppler subprocess) instead of one call per page --
            was the actual bottleneck causing long stalls after download
            hit 100% (19 pages = 19 separate subprocess spins). Falls back
            to per-page conversion only if the batch call itself fails."""
            try:
                imgs = convert_from_bytes(pdf_bytes, first_page=first, last_page=last, dpi=150, thread_count=4)
                if imgs and len(imgs) == (last - first + 1):
                    return imgs
            except Exception as _conv_e:
                logger.warning(f"[PDF] Batch convert pages {first}-{last} (dpi=150) raised: {_conv_e}")
            return None

        def _convert_one_page(p):
            # PERMANENT FIX: a page must never be silently dropped just
            # because a convert_from_bytes call returned empty (transient
            # poppler hiccup, momentary resource blip, etc). 5 attempts with
            # progressive backoff at dpi=150, then a last-ditch dpi=100
            # fallback (some pages fail to render at higher dpi due to
            # memory/complexity but succeed at lower dpi).
            imgs = None
            backoffs = [1, 2, 3, 4]
            for _attempt in range(5):
                try:
                    imgs = convert_from_bytes(pdf_bytes, first_page=p, last_page=p, dpi=150, thread_count=4)
                    if imgs:
                        return imgs[0]
                except Exception as _conv_e:
                    logger.warning(f"[PDF] Page {p} convert attempt {_attempt+1}/5 (dpi=150) raised: {_conv_e}")
                if _attempt < len(backoffs):
                    time.sleep(backoffs[_attempt])
            try:
                imgs = convert_from_bytes(pdf_bytes, first_page=p, last_page=p, dpi=100, thread_count=4)
                if imgs:
                    logger.warning(f"[PDF] Page {p} recovered via dpi=100 fallback after 5 failed dpi=150 attempts")
                    return imgs[0]
            except Exception as _conv_e:
                logger.warning(f"[PDF] Page {p} dpi=100 fallback attempt also raised: {_conv_e}")
            return None

        if page_range:
            parts = page_range.split("-")
            first = int(parts[0])
            last = int(parts[1]) if len(parts) > 1 else first
            if last - first + 1 > _PDF_MAX_PAGES_PER_CALL:
                raise ValueError(
                    f"PDF_RANGE_TOO_LARGE:{first}:{last}:{_PDF_MAX_PAGES_PER_CALL}"
                )
            # BUG FIX: requested range could exceed the PDF's real page count
            # (e.g. range 505-508 on a 504-page PDF). Without this check every
            # non-existent page burns 5 retry attempts + dpi-100 fallback each
            # (pure wasted time), then gets a blank white placeholder inserted,
            # which Gemini/Groq then "sees" as a real empty page and returns
            # zero MCQs for it -- surfacing as a confusing "কোনো MCQ পাওয়া যায়নি"
            # instead of a clear out-of-range error.
            _total_pages_check = get_pdf_page_count(pdf_bytes)
            if _total_pages_check and first > _total_pages_check:
                raise ValueError(
                    f"PDF_RANGE_OUT_OF_BOUNDS:{first}:{last}:{_total_pages_check}"
                )
            if _total_pages_check and last > _total_pages_check:
                logger.warning(
                    f"[PDF] Requested range {first}-{last} exceeds real page count "
                    f"({_total_pages_check}) — clamping to {first}-{_total_pages_check}"
                )
                last = _total_pages_check
            batch = _convert_batch(first, last)
            if batch is not None:
                result = list(zip(range(first, last + 1), batch))
                logger.info(f"[PDF] Converted {len(result)} pages (single batch call)")
                return result
            # Batch failed -- fall back to the slower but bulletproof
            # per-page path (with its own retry/dpi-fallback) so no page
            # is ever silently dropped.
            result = []
            missing_pages = []
            for p in range(first, last + 1):
                img = _convert_one_page(p)
                if img is not None:
                    result.append((p, img))
                else:
                    missing_pages.append(p)
                    logger.error(f"[PDF] Page {p} FAILED to convert after all retries+dpi-fallback — inserting placeholder so page is NEVER skipped/dropped.")
                    result.append((p, Image.new("RGB", (1240, 1754), "white")))
            if missing_pages:
                logger.error(f"[PDF] UNRECOVERABLE render failure (placeholder inserted) for pages: {missing_pages} (out of range {first}-{last})")
            logger.info(f"[PDF] Converted {len(result)} pages (per-page fallback)")
            return result
        else:
            total_pages = get_pdf_page_count(pdf_bytes)
            if total_pages:
                cap = min(total_pages, _PDF_MAX_PAGES_PER_CALL)
                batch = _convert_batch(1, cap)
                if batch is not None:
                    result = list(zip(range(1, cap + 1), batch))
                    if total_pages > _PDF_MAX_PAGES_PER_CALL:
                        raise ValueError(f"PDF_TRUNCATED_AT:{_PDF_MAX_PAGES_PER_CALL}")
                    logger.info(f"[PDF] Converted {len(result)} pages (single batch call)")
                    return result
            # REAL BUG FIX: previously an empty convert_from_bytes result on
            # page N was treated as "end of document" via break — but empty
            # can also mean a TRANSIENT failure on a page that is NOT actually
            # the last page, silently truncating the rest of the PDF. Now we
            # retry+dpi-fallback per page, and only treat it as true
            # end-of-document once confirmed against the real page count.
            result = []
            missing_pages = []
            p = 1
            while p <= _PDF_MAX_PAGES_PER_CALL:
                img = _convert_one_page(p)
                if img is None:
                    if total_pages and p <= total_pages:
                        missing_pages.append(p)
                        logger.error(f"[PDF] Page {p} FAILED to convert after all retries+dpi-fallback (total_pages={total_pages}) — inserting placeholder so page is NEVER skipped/dropped.")
                        result.append((p, Image.new("RGB", (1240, 1754), "white")))
                        p += 1
                        continue
                    else:
                        break  # true end of document (or page count unknown)
                result.append((p, img))
                p += 1
            if p > _PDF_MAX_PAGES_PER_CALL:
                extra = _convert_one_page(p)
                if extra is not None:
                    raise ValueError(f"PDF_TRUNCATED_AT:{_PDF_MAX_PAGES_PER_CALL}")
            if missing_pages:
                logger.error(f"[PDF] UNRECOVERABLE render failure (placeholder inserted) for pages: {missing_pages} (total_pages={total_pages})")
            logger.info(f"[PDF] Converted {len(result)} pages (per-page fallback)")
            return result
    except Exception as e:
        logger.error(f"[PDF] Convert error: {e}")
        raise
    finally:
        _PDF_CONVERT_LOCK.release()


def get_pdf_page_count(pdf_bytes: bytes) -> int:
    """Lightweight page count (no rasterization, minimal RAM) — used for auto-chunking."""
    try:
        from pypdf import PdfReader
        return len(PdfReader(BytesIO(pdf_bytes)).pages)
    except Exception as e:
        logger.warning(f"[PDF] page count failed: {e}")
        return 0

def pdf_to_images_safe(pdf_bytes: bytes, page_range: str = None):
    """Wrapper for pdf_to_images() that turns the RAM-safety exceptions into
    a friendly (ok: bool, result) tuple instead of a raw crash/traceback to
    the user -- result is the page list on success, or a Bengali user-facing
    error string on failure (queue busy / PDF too large / range too large)."""
    try:
        return True, pdf_to_images(pdf_bytes, page_range)
    except ValueError as e:
        msg = str(e)
        if msg.startswith("PDF_TRUNCATED_AT:"):
            cap = msg.split(":")[1]
            return False, (f"❌ PDF-টি {cap} page-এর বেশি! RAM safety-র জন্য একসাথে সর্বোচ্চ "
                            f"{cap} page process করা যায়।\nদয়া করে page range দিয়ে ভাগ করে পাঠাও "
                            f"(যেমন: pages 1-{cap}, তারপর {int(cap)+1}-{int(cap)*2})।")
        if msg.startswith("PDF_RANGE_TOO_LARGE:"):
            _, first, last, cap = msg.split(":")
            return False, (f"❌ এই range-এ {int(last)-int(first)+1} page, কিন্তু সর্বোচ্চ {cap} page "
                            f"একসাথে process করা যায়।\nদয়া করে ছোট range দিয়ে আবার চেষ্টা করো।")
        if msg.startswith("PDF_RANGE_OUT_OF_BOUNDS:"):
            _, first, last, total = msg.split(":")
            return False, (f"❌ PDF-এ মোট {total} page আছে, কিন্তু তুমি {first}-{last} চেয়েছো।\n"
                            f"দয়া করে সঠিক page range দিয়ে আবার চেষ্টা করো (১-{total} এর মধ্যে)।")
        return False, f"❌ PDF process করতে সমস্যা হয়েছে: {msg}"
    except RuntimeError:
        return False, "⏳ Server এখন busy (অন্য একটা PDF process হচ্ছে), কিছুক্ষণ পর আবার চেষ্টা করো।"
    except Exception as e:
        logger.error(f"[PDF] pdf_to_images_safe unexpected error: {e}")
        return False, "❌ PDF process করতে সমস্যা হয়েছে।"

# ============================================================
# IMAGE HELPERS
# ============================================================
def image_to_base64(img: Image.Image) -> str:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()

def image_to_bytes(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()

# ============================================================
# JSON PARSE HELPER (shared)
# ============================================================
def _strip_q_numbering(q: str) -> str:
    """প্রশ্নের শুরুতে numbering prefix (1) 14) 1. Q1. ইত্যাদি) সরায়।"""
    if not q:
        return q
    # NOTE: negative lookahead (?!\d) prevents stripping the leading digit of
    # a decimal number (e.g. "0.05M" must not become "05M").
    pattern = r'^\s*(?:[Qq]\.?\s*)?[\d১২৩৪৫৬৭৮৯০]{1,3}\s*[).।:.\-](?!\d)\s*'
    cur = q
    for _ in range(2):
        new = re.sub(pattern, '', cur)
        if new == cur:
            break
        cur = new
    return cur.strip()

def _parse_mcq_json(text: str) -> list:
    text = text.strip()
    # Reasoning models sometimes prefix output with <think>...</think> —
    # strip it before markdown-fence handling / json.loads.
    if "<think>" in text:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        if not text or "<think>" in text:
            raise ValueError("Unclosed <think> block, no usable JSON")
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    mcqs = json.loads(text)
    if not isinstance(mcqs, list) or len(mcqs) == 0:
        raise ValueError("Empty MCQ list")
    valid = []
    _nav_label_re = re.compile(r'^(card|page|section|chapter|part|topic|slide)\s*\d*$', re.IGNORECASE)
    for m in mcqs:
        if all(k in m for k in ["question", "options", "answer", "explanation"]):
            if len(m["options"]) == 4 and m["answer"] in ["A", "B", "C", "D"]:
                m["question"] = _strip_q_numbering(str(m.get("question", "")))
                # Defense-in-depth: navigation-label-like options (e.g. "Card 1",
                # "Section 2") indicate the AI leaked page-structure text into the
                # options instead of real content — reject this MCQ entirely.
                if any(_nav_label_re.match(str(o).strip()) for o in m["options"]):
                    logger.warning(f"[MCQ] Rejected — nav-label option detected: {m['options']}")
                    continue
                valid.append(m)

    # Post-process: answer গুলো সব একই হলে shuffle করো
    import random as _rnd
    if valid:
        answers = [m["answer"] for m in valid]
        # সব answer একই হলে force distribute
        if len(set(answers)) == 1:
            labels = ["A", "B", "C", "D"]
            for i, m in enumerate(valid):
                new_ans_label = labels[i % 4]
                new_ans_idx = labels.index(new_ans_label)
                old_ans_idx = labels.index(m["answer"])
                opts = m["options"][:]
                # correct option swap করো new position এ
                opts[old_ans_idx], opts[new_ans_idx] = opts[new_ans_idx], opts[old_ans_idx]
                m["options"] = opts
                m["answer"] = new_ans_label

    return valid


def crop_option_image(img: Image.Image, bbox: list) -> str:
    """
    /qbm option-image support: a REAL tight crop of just the image sitting on
    an MCQ option — unlike crop_explanation_image() (which uploads the full
    page with a highlight overlay for CSS-cropped client rendering), this
    uploads ONLY the cropped region itself, since it's embedded as a normal
    <img> tag directly inside that option's CSV/DB text.
    Returns the uploaded url, or "" on failure.
    """
    if not bbox or len(bbox) != 4:
        return ""
    try:
        from atlas_mhtml import upload_to_imgbb
        w, h = img.size
        x_min, y_min, x_max, y_max = bbox
        # padding (2.5% of each dimension) so diagram edges/labels never get cut
        pad_x, pad_y = w * 0.025, h * 0.025
        left = max(0, int((x_min / 1000) * w - pad_x))
        top = max(0, int((y_min / 1000) * h - pad_y))
        right = min(w, int((x_max / 1000) * w + pad_x))
        bottom = min(h, int((y_max / 1000) * h + pad_y))
        if right <= left or bottom <= top:
            return ""
        crop = img.convert("RGB").crop((left, top, right, bottom))
        url = upload_to_imgbb(image_to_base64(crop))
        if not url:
            url = upload_to_imgbb(image_to_base64(crop))  # one retry
        return url or ""
    except Exception as e:
        logger.warning(f"[OptionImageCrop] Failed: {e}")
        return ""


def crop_explanation_image(img: Image.Image, bbox: list) -> dict:
    """
    Single upload (full page, red-border marked at exp_box). Client renders
    a CSS-cropped thumbnail view (object-position) using bbox percentages,
    and full image on click — avoids double-upload fragility.
    Returns {"url": str, "top_pct": float, "bottom_pct": float}.
    """
    if not bbox or len(bbox) != 4:
        return {}
    try:
        from atlas_mhtml import upload_to_imgbb
        from PIL import ImageDraw
        w, h = img.size
        x_min, y_min, x_max, y_max = bbox
        box_top = (y_min / 1000) * h
        box_bottom = (y_max / 1000) * h
        box_top = (y_min / 1000) * h
        box_bottom = (y_max / 1000) * h

        # Snap top/bottom to nearest blank row so text isn't cut mid-line
        gray = img.convert("L")
        import numpy as np
        arr = np.asarray(gray, dtype=np.uint8)
        row_min = arr.min(axis=1)
        max_extend = int(h * 0.12)

        def _snap_top(y0):
            y = int(y0)
            limit = max(0, y - max_extend)
            i = y
            while i > limit and row_min[i] < 245:
                i -= 1
            return max(0, i)

        def _snap_bottom(y0):
            y = int(y0)
            limit = min(h - 1, y + max_extend)
            i = y
            while i < limit and row_min[i] < 245:
                i += 1
            return min(h, i + 1)

        b_top = max(0, int(box_top))
        b_bottom = min(h, int(box_bottom))

        # Single upload (full page, orange highlight + red box) — avoids the
        # double-upload fragility that was causing MCQs to get skipped on
        # imgbb timeouts/failures.
        full_img = img.convert("RGB").copy()
        if b_bottom > b_top:
            overlay = full_img.copy()
            ov_draw = ImageDraw.Draw(overlay)
            ov_draw.rectangle([0, b_top, w, b_bottom], fill=(255, 165, 0))
            full_img = Image.blend(full_img, overlay, 0.35)
            draw = ImageDraw.Draw(full_img)
            draw.rectangle([6, b_top + 6, w - 6, max(b_top + 7, b_bottom - 6)], outline=(220, 38, 38), width=8)
        url = upload_to_imgbb(image_to_base64(full_img))
        if not url:
            url = upload_to_imgbb(image_to_base64(full_img))  # one retry
        if not url:
            return {}
        return {
            "url": url,
            "top_pct": round((b_top / h) * 100, 2),
            "bottom_pct": round((b_bottom / h) * 100, 2),
        }
    except Exception as e:
        logger.warning(f"[ExplanationCrop] Failed: {e}")
        return {}

# ============================================================
# OPENROUTER FALLBACK — Qwen2.5-VL
# ============================================================
OPENROUTER_MODELS = [
    m.strip() for m in
    os.environ.get("OPENROUTER_MODELS",
        "google/gemma-4-31b-it:free,google/gemma-4-26b-a4b-it:free,nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free,dots-studio/dots-3-note-preview:free"
    ).split(",") if m.strip()
]

async def _openrouter_fallback(img: Image.Image, prompt: str, page: int) -> list:
    if not openrouter_rotator.has_keys():
        logger.warning("[OpenRouter] No keys available, skipping fallback")
        return []

    img_b64 = image_to_base64(img)
    max_retries = min(len(openrouter_rotator.keys) * len(OPENROUTER_MODELS), 4)

    for attempt in range(max(max_retries, 3)):
        model = OPENROUTER_MODELS[attempt % len(OPENROUTER_MODELS)]
        try:
            _ordered = openrouter_rotator.ordered_keys()
            key = _ordered[attempt % len(_ordered)] if _ordered else openrouter_rotator.get_key()
            logger.info(f"[OpenRouter] Attempt {attempt+1}, model: {model}")

            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "HTTP-Referer": "https://atlascourses.com",
                        "X-Title": "ATLAS MCQ Bot"
                    },
                    json={
                        "model": model,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {
                                    "url": f"data:image/jpeg;base64,{img_b64}"
                                }}
                            ]
                        }],
                        "max_tokens": 4096
                    }
                )

            if r.status_code == 429:
                logger.warning(f"[OpenRouter] Rate limit on attempt {attempt+1}, cooling down key for {openrouter_rotator.COOLDOWN_SECONDS}s, retrying...")
                openrouter_rotator.mark_rate_limited(key)
                await asyncio.sleep(2)
                continue

            if r.status_code != 200:
                logger.warning(f"[OpenRouter] HTTP {r.status_code} on attempt {attempt+1}")
                await asyncio.sleep(1)
                continue

            data = r.json()
            text = data["choices"][0]["message"]["content"]
            valid = _parse_mcq_json(text)
            openrouter_rotator.mark_healthy(key)
            logger.info(f"[OpenRouter] Page {page}: {len(valid)} MCQs via {model}")
            return valid

        except Exception as e:
            logger.warning(f"[OpenRouter] Attempt {attempt+1} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
            continue

    logger.error(f"[OpenRouter] All attempts failed for page {page}")
    return []

# ============================================================
# GENERATE MCQ FROM IMAGE — Gemini primary + OpenRouter fallback
# ============================================================
async def generate_mcq_from_image(
    img: Image.Image,
    topic: str,
    page: int,
    mcq_count: int = None,
    max_keys: int = None,
    custom_prompt: str = None,
) -> list:
    if custom_prompt:
        # /tf (and any other caller with its own fully-formed prompt) skips
        # the default MCQ_PROMPT_WITH_COUNT/MCQ_PROMPT_MAX templating below
        # entirely and reuses THIS SAME battle-tested Gemini/Groq provider
        # chain (key rotation, model fallback, MAX_TOKENS detection,
        # exponential backoff) with its own prompt text instead.
        prompt = custom_prompt
    elif isinstance(mcq_count, (tuple, list)) and len(mcq_count) == 2:
        c_min, c_max = mcq_count
        range_rule = (
            f"STRICT RANGE REQUIRED: Extract BETWEEN {c_min} AND {c_max} MCQs from "
            f"this page — no fewer than {c_min}, no more than {c_max}. Hard rule, "
            f"not a suggestion."
        )
        prompt = MCQ_PROMPT_WITH_COUNT.format(
            count=f"{c_min}-{c_max} ({range_rule})", topic=topic, page=str(page).zfill(2)
        )
    elif mcq_count:
        prompt = MCQ_PROMPT_WITH_COUNT.format(
            count=mcq_count, topic=topic, page=str(page).zfill(2)
        )
    else:
        prompt = MCQ_PROMPT_MAX.format(topic=topic, page=str(page).zfill(2))

    # ── PRIMARY: Gemini ──────────────────────────────────────
    # v4.5: previously tried EVERY configured key at a full 45s timeout each —
    # with 5-6 keys that's 4-5 minutes of stalling per image before ever
    # reaching the OpenRouter fallback. Cap attempts at 3 keys max, and use a
    # shorter timeout on the 2nd/3rd attempt so a bad/slow key fails fast.
    _ordered = key_rotator.ordered_keys(offset=_qbm_key_offset_ctx.get())
    # 2026-08-28 (user request): multi-round Gemini/Gemma interleaving --
    # caller can cap this round to max_keys, so the outer loop in app.py can
    # do "10 Gemini keys -> Gemma -> 5 more Gemini keys -> Gemma -> remaining
    # Gemini keys -> Groq" instead of always burning every live key before
    # ever trying Gemma. Deliberately NOT slicing by key_start_index here --
    # ordered_keys() is called fresh each round and already sorts healthy
    # keys first (any key that failed in an earlier round gets mark_rate_
    # limited() and sinks in THIS call automatically), so re-deriving the
    # order fresh always tries the current healthiest keys first rather than
    # a stale index offset that could re-surface an already-failed key.
    # Only attempt keys not already known-exhausted/banned today — retrying
    # a dead key wastes a full timeout slot for nothing, and skipping them
    # lets us cycle through ALL live keys within max_retries.
    _all_marked_exhausted = bool(key_rotator.keys) and all(_is_gemini_key_exhausted_today(k) for k in key_rotator.keys)
    _live = [k for k in _ordered if not _is_gemini_key_exhausted_today(k)]
    if _live:
        _ordered = _live
    elif _all_marked_exhausted:
        # Don't blind-trust "every key exhausted" -- 40+ independent keys
        # genuinely hitting quota at the exact same moment is implausible;
        # this is much more likely a bad flag (D1 rehydrate glitch,
        # misclassified error, stale date compare). Verify with 2 real
        # attempts before falling to OpenRouter/Groq.
        logger.warning(f"[Gemini] all {len(key_rotator.keys)} keys marked daily-exhausted (suspicious) — retrying 2 keys for real before giving up")
        _ordered = _ordered[:2] if _ordered else []

    # User instruction (2026-08-25): Gemini has many live keys -- Groq
    # should NEVER be touched while even one Gemini key is still alive
    # (not daily-exhausted). Try every live key before falling through.
    # Exception: if the Gemini BACKEND itself is down (not a per-key quota
    # issue -- e.g. network/503 outage), every key fails the same way, so
    # burning all of them (could be dozens) before falling back would stall
    # a single page for many minutes. Detect that case specifically: if the
    # first 3 consecutive attempts ALL fail with a timeout/connection/503
    # error (not quota/429/invalid-key), treat it as a backend outage and
    # stop early instead of exhausting the whole key list pointlessly.
    max_retries = len(_ordered) if _ordered else 5
    if max_keys is not None:
        # 2026-08-28: cap this round to max_keys so the caller's multi-round
        # Gemini/Gemma interleave loop controls how many keys get tried
        # before returning control (empty list if none of this round's keys
        # succeed -- caller decides whether to try Gemma or another round).
        max_retries = min(max_retries, max_keys)
    _consecutive_infra_fails = 0
    # Model fallback chain: try the latest model first, and if the WHOLE
    # Gemini backend for it is overloaded (503 UNAVAILABLE — this is a
    # server-side capacity issue, not a per-key problem, so it hits every
    # key the same way), drop to the older stable model on the same key
    # before moving to the next key. New models get more 503s in their
    # first weeks of traffic ramp-up.
    # 2026-08-07: switched back to gemini-3.5-flash — gemini-3.5-flash was
    # 404ing ("no longer available to new users") for new API keys, on top
    # of its own daily-quota exhaustion, so it's no longer a safe primary.
    _GEMINI_MODELS = ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-2.5-flash"]

    _tried_keys = set()
    for attempt in range(max_retries):
        # BUG FIX: previously indexed into the `_ordered` snapshot taken
        # BEFORE the loop started (attempt % len(_ordered)). As attempts
        # fail and mark_rate_limited()/mark_banned() run below, that stale
        # snapshot never updated -- so later attempts kept cycling back onto
        # keys already just cooled-down/banned in THIS same loop (wasting a
        # retry slot on a known-bad key) while a genuinely healthy key
        # sitting elsewhere in the live pool could go completely untried
        # whenever max_keys capped max_retries below the full key count.
        # Re-deriving ordered_keys() fresh each attempt (already cheap --
        # same call used to build the original _ordered) always reflects
        # the current healthy-first order and skips keys already tried
        # this round before falling back to a repeat if truly none remain.
        _fresh = key_rotator.ordered_keys(offset=_qbm_key_offset_ctx.get())
        _fresh = [k for k in _fresh if not _is_gemini_key_exhausted_today(k)] or _fresh
        _untried = [k for k in _fresh if k not in _tried_keys]
        if _untried:
            key = _untried[0]
        elif _fresh:
            key = _fresh[attempt % len(_fresh)]
        else:
            key = _ordered[attempt % len(_ordered)] if _ordered else key_rotator.get_key()
        _tried_keys.add(key)
        key_rotator.record_call(key)
        last_exc = None
        for model_name in _GEMINI_MODELS:
            try:
                from google import genai as gai
                from google.genai import types
                # FIX (2026-08-23): the client had no HTTP-level timeout, so
                # asyncio.wait_for() only stopped *waiting* on the outer
                # coroutine -- the underlying request in the background
                # thread kept running for the SDK's own (much larger)
                # default, meaning every attempt silently burned its full
                # 40s/25s budget instead of failing fast. Setting an
                # explicit http_options timeout (slightly under our
                # asyncio timeout, in ms) lets the real network call give
                # up on its own so the retry loop can move to the next key
                # promptly instead of stalling on a dead/slow connection.
                _http_timeout_ms = 45000 if attempt == 0 else 28000
                client = gai.Client(
                    api_key=key,
                    http_options=types.HttpOptions(timeout=_http_timeout_ms)
                )
                img_b64 = image_to_base64(img)

                def _call_gemini():
                    return client.models.generate_content(
                        model=model_name,
                        contents=[
                            types.Part.from_text(text=prompt),
                            types.Part.from_bytes(
                                data=base64.b64decode(img_b64),
                                mime_type="image/jpeg"
                            )
                        ],
                        # 2026-08-29: right-sized for /math's actual target
                        # (20-25 MCQs/page, safety ceiling 40) -- 25 dense
                        # MCQs with full formula+step explanations run
                        # roughly 6k-11k tokens; 16384 gives comfortable
                        # headroom up to the 40-MCQ ceiling without the
                        # excess that was pushing generation past Gemini's
                        # fixed deadline (was 32768, then 24576).
                        config=types.GenerateContentConfig(max_output_tokens=16384)
                    )

                # 2026-08-29: 16384 token cap (right-sized for 20-25 MCQs)
                # generates faster than the earlier 24576/32768 caps did --
                # 50/32s gives enough margin without holding a slow/dead
                # key too long, keeping the fast+accurate balance for the
                # common case while still tolerating occasional slower
                # generations for pages near the 40-MCQ ceiling.
                _attempt_timeout = 50 if attempt == 0 else 32
                async with key_rotator.throttled_call(key=key):
                    response = await asyncio.wait_for(asyncio.to_thread(_call_gemini), timeout=_attempt_timeout)
                # 2026-08-28: detect a response that got cut off by the
                # max_output_tokens cap above (MAX_TOKENS finish_reason) --
                # a truncated response is usually broken/partial JSON (cut
                # mid-key or mid-string) that _parse_mcq_json's repair logic
                # can't always recover, and silently accepting it risks
                # returning fewer/mangled MCQs instead of retrying with a
                # fresh key the way a real technical failure would.
                try:
                    _finish_reason = response.candidates[0].finish_reason if response.candidates else None
                except Exception:
                    _finish_reason = None
                if _finish_reason is not None and "MAX_TOKENS" in str(_finish_reason).upper():
                    logger.warning(f"[Gemini] Page {page}: response hit max_output_tokens (truncated) on attempt {attempt+1} — treating as technical failure, trying next key")
                    last_exc = RuntimeError("MAX_TOKENS truncation")
                    continue
                valid = _parse_mcq_json(response.text)
                if not valid:
                    try:
                        from app import record_empty_parse
                        record_empty_parse("gemini")
                    except Exception:
                        pass
                    logger.warning(f"[Gemini] Page {page}: response OK but 0 valid MCQs parsed (attempt {attempt+1}, model={model_name}) — likely malformed/truncated JSON, not a real 'page has no content'")
                key_rotator.mark_healthy(key)
                logger.info(f"[Gemini] Page {page}: {len(valid)} MCQs (attempt {attempt+1}, model={model_name})")
                return valid
            except Exception as e:
                last_exc = e
                err_str = str(e)
                if "503" in err_str or "UNAVAILABLE" in err_str.upper():
                    try:
                        from app import classify_ai_error
                        classify_ai_error(e, "gemini")
                    except Exception:
                        pass
                    logger.warning(f"[Gemini] {model_name} overloaded (503) on attempt {attempt+1} — trying next model on same key")
                    continue
                # Not a 503 — no point trying the fallback model on this key,
                # move straight to key-level handling below.
                break
        e = last_exc
        if e is None:
            continue  # shouldn't happen, but guard just in case
        err_str = _gemini_full_error_text(e)
        err_label = f"{type(e).__name__}: {err_str}" if err_str else f"{type(e).__name__} (no message — likely timeout)"
        try:
            from app import classify_ai_error
            classify_ai_error(e, "gemini")
        except Exception:
            pass  # classifier is best-effort visibility only, never blocks the real retry logic
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
            is_daily = "PerDay" in err_str or "generate_content_free_tier_requests" in err_str
            if is_daily:
                logger.warning(f"[Gemini] Attempt {attempt+1}: daily quota exhausted — skipping this key until Pacific-day quota reset: {err_label}")
            else:
                logger.warning(f"[Gemini] Attempt {attempt+1} rate-limited (429), cooling down key for {key_rotator.COOLDOWN_SECONDS}s: {err_label}")
            key_rotator.mark_rate_limited(key, daily_exhausted=is_daily)
            _consecutive_infra_fails = 0  # quota issue is per-key, not backend-wide -- keep trying other live keys
        elif ("SUSPENDED" in err_str.upper() or "API_KEY_INVALID" in err_str.upper()
              or "UNAUTHENTICATED" in err_str.upper() or "ACCOUNT_STATE_INVALID" in err_str.upper()
              or "401" in err_str):
            logger.error(f"[Gemini] Attempt {attempt+1}: key permanently banned (suspended/invalid): {err_label}")
            key_rotator.mark_banned(key, reason=err_str[:200]); key_rotator.record_account_error(key)
            _consecutive_infra_fails = 0  # per-key issue, not backend-wide
        else:
            # Timeout / connection error / non-429-503 exception. Previously
            # this broke early after 3 consecutive failures assuming Gemini's
            # backend was down entirely -- but per explicit instruction,
            # Gemini must be exhausted key-by-key before ever falling to
            # Groq/other providers, so we no longer short-circuit here and
            # instead keep cycling through every remaining live key.
            logger.warning(f"[Gemini] Attempt {attempt+1} failed (both models): {err_label}")
            _consecutive_infra_fails += 1
        if attempt < max_retries - 1:
            # 2026-08-28 (user request): exponential backoff on transient
            # infra failures (timeout/503/504-style) instead of a flat 1s
            # delay -- 2s -> 5s -> 10s -> capped at 10s, so repeated retries
            # give Google's backend more breathing room during a real
            # overload instead of hammering it every second. 429/401 skip
            # this (they already cooldown/ban the specific key above and
            # move to a different key immediately, no benefit from delay).
            _is_infra_fail = not ("429" in err_str or "RESOURCE_EXHAUSTED" in err_str
                                   or "quota" in err_str.lower()
                                   or "SUSPENDED" in err_str.upper() or "API_KEY_INVALID" in err_str.upper()
                                   or "UNAUTHENTICATED" in err_str.upper() or "401" in err_str)
            if _is_infra_fail:
                _backoff_schedule = [2, 5, 10]
                _backoff_s = _backoff_schedule[min(_consecutive_infra_fails - 1, len(_backoff_schedule) - 1)] if _consecutive_infra_fails > 0 else 2
                await asyncio.sleep(_backoff_s)
            else:
                await asyncio.sleep(1)
        continue

    logger.warning(f"[Gemini] All keys failed for page {page} — returning empty (caller will try Groq/other fallbacks)")

    # NOTE: OpenRouter fallback removed from here (2026-08) — this function
    # is called as the PRIMARY step from app.py's _generate_mcq_from_image_raw,
    # which itself falls to Groq next, then to _AI_PROVIDERS_ORDER (nvidia/
    # openrouter_qwen/nemotron/gemma/hf). Calling OpenRouter here too meant
    # Groq was being skipped entirely whenever Gemini failed, since this
    # function would already return a (possibly empty) OpenRouter result
    # before app.py ever got a chance to try Groq.
    return []



async def generate_mcq_from_text(text: str, topic: str = "MCQ", count: int = 15) -> list:
    # 2026-08-22 BUGFIX: previously wrapped in MCQ_PROCESSING_QUEUE_LOCK, a
    # single global asyncio.Lock -- meant only ONE /txt job could run across
    # THE WHOLE BOT at once, so User B's /txt request sat fully blocked
    # behind User A's until A's finished, even though each call already uses
    # its own independent Gemini/Groq API key + connection and has nothing
    # that actually needs to be serialized. Removed: each call now runs
    # concurrently, so multiple users' /txt jobs never queue behind each other.
    return await _generate_mcq_from_text_raw(text, topic, count)


async def _generate_mcq_from_text_raw(text: str, topic: str = "MCQ", count: int = 15) -> list:
    """Text থেকে MCQ generate করে — same SDK + multi-key + fallback as generate_mcq_from_image"""
    import json as _json

    prompt = f"""তুমি একজন expert MCQ writer। নিচের text-টি লাইন-বাই-লাইন সম্পূর্ণ পড়ো এবং QUALITY বজায় রেখে MCQ বানাও। সংখ্যা কোনো target না — শর্ত মেনে যতগুলো ভালো MCQ বানানো সম্ভব ঠিক ততগুলোই বানাবে, বেশি দেখানোর জন্য জোর করে কম মানের MCQ বানাবে না।

MANDATORY RULES (কোনোটাই skip করা যাবে না):
0. STRICT SOURCE-ONLY RULE: শুধুমাত্র নিচের TEXT-এ যা লেখা আছে সেখান থেকেই MCQ বানাতে হবে। Text-এ নেই এমন কোনো তথ্য, fact, নাম, সংখ্যা নিজে থেকে বানানো/অনুমান করা সম্পূর্ণ নিষেধ। প্রশ্ন ও option ঘুরিয়ে-পেঁচিয়ে (rephrase করে) লেখা যাবে, কিন্তু অর্থ/তথ্য অবশ্যই মূল text থেকেই আসতে হবে — বাইরের কোনো knowledge ব্যবহার করা যাবে না।
1. MANDATORY: Text-এর প্রতিটি লাইন/তথ্যপূর্ণ vakko থেকে অবশ্যই কমপক্ষে একটি MCQ বানাতে হবে — কোনো লাইন বাদ দেওয়া যাবে না (শুধু pure heading/tag/navigation line ছাড়া, যেগুলোতে কোনো factual তথ্যই নেই)। কোনো লাইন সংক্ষিপ্ত/সাধারণ মনে হলেও সেটা থেকে rephrase/context ব্যবহার করে MCQ বানানোর সর্বোচ্চ চেষ্টা করবে।
2. এরপর কয়েকটা লাইনের তথ্য মিক্স/combine করে additional MCQ বানাবে — যেখানে প্রশ্ন বা option একাধিক লাইনের তথ্য একসাথে ব্যবহার করে (যেমন দুইটা ভিন্ন লাইনের ফ্যাক্ট মিলিয়ে comparison/relation ভিত্তিক প্রশ্ন)।
2a. এর মধ্যে অন্তত ৩-৫টি MCQ এমন হবে যেখানে একটা single প্রশ্নের মধ্যেই বেশ কয়েকটা আলাদা তথ্য (multiple facts) একসাথে verify করা যায় — যেমন option-গুলো নিজেরাই ২-৩টা তথ্যের combination হবে, আর সঠিক option-টাই একমাত্র যেটা সবগুলো তথ্য মিলিয়ে সঠিক। এগুলো extreme-level কঠিন হবে না — মাঝারি (moderate) কঠিন রাখবে, যাতে মনোযোগ দিয়ে পড়লে বোঝা যায়, কিন্তু অতিরিক্ত ঘুরিয়ে-প্যাঁচিয়ে confusing না হয়।
3. এছাড়াও পুরো text থেকে overall বুঝে কিছু brainstorming MCQ বানাবে — একাধিক তথ্য যুক্তি দিয়ে সংযুক্ত করে গভীর প্রশ্ন (এখনও strictly text-এর তথ্যের ভিত্তিতেই, বাইরের knowledge না)।
3a. এদের মধ্যে কিছু MCQ ইচ্ছাকৃতভাবে "কঠিন/verification-type" হতে হবে — যেগুলো শুধু sample/superficial পড়লে উত্তর দেওয়া যাবে না, বরং পুরো text মনোযোগ দিয়ে ভালোভাবে পড়লেই সঠিক উত্তর দেওয়া সম্ভব হবে (যেমন: দুইটা কাছাকাছি/similar তথ্যের মধ্যে সূক্ষ্ম পার্থক্য ধরিয়ে দেওয়া, ব্যতিক্রম/exception ধরনের তথ্য, একাধিক শর্ত একসাথে মেলানো, বা easily-confused নাম/সংখ্যার মধ্যে সঠিকটা বাছাই)। এগুলো extreme-level কঠিন হবে না, শুধু moderate — এগুলো দিয়ে বোঝা যাবে ইউজার সত্যিই মনোযোগ দিয়ে পুরো text পড়েছে কি না।
4. Explanation-এ সঠিক answer confirm করার পাশাপাশি সংশ্লিষ্ট তথ্যের ঠিক আশেপাশের (আগের/পরের লাইনের) অতিরিক্ত related info যোগ করতে হবে — শুধু answer repeat করা চলবে না। এছাড়া explanation-এ বাকি ৩টা ভুল option কেন ভুল/কী সেটার ছোট ব্যাখ্যাও থাকবে, যাতে ইউজার প্রতিটা option সম্পর্কেই বুঝতে পারে (নিচের rule 4b তে explanation-এর length constraint বিস্তারিত আছে)।
4a. STRICTLY NISHIDDHO (প্রশ্ন এবং explanation দুই জায়গাতেই): "টেক্সট অনুসারে", "টপিক অনুসারে", "টেক্সটে লিখা আছে", "উপরের তথ্য অনুযায়ী", "প্রদত্ত অংশে বলা হয়েছে", "উক্ত অনুচ্ছেদে উল্লেখ আছে", "টপিকে বলা হয়েছে", "দেখা যাচ্ছে", "লিখা আছে", "বর্ণিত আছে" বা এই জাতীয় কোনো source/reference-উল্লেখকারী কথা প্রশ্ন কিংবা explanation কোথাও কখনোই লেখা যাবে না। প্রশ্ন ও explanation দুটোই সরাসরি fact-টুকু বলবে, কোনো source-এর দিকে ইঙ্গিত করবে না।
4b. EXPLANATION LENGTH RULE (Telegram poll explanation box limit মাথায় রেখে): ভুল ৩টা option-এর সংক্ষিপ্ত ব্যাখ্যা অবশ্যই এই box-এর character limit-এর মধ্যে রাখতে হবে (মোট explanation ~200 characters-এর মধ্যে গুছিয়ে রাখার চেষ্টা করবে) — সংক্ষেপে, এক লাইনে বলবে কেন ভুল। সঠিক answer নিয়ে extra info তথ্যবহুল হলে সামান্য limit-এর বাইরে যেতে পারে, কিন্তু চেষ্টা করবে সবটাই যতটা সম্ভব সংক্ষিপ্ত ও গোছানো রাখতে।
5. সঠিক answer (A/B/C/D) প্রতিটি প্রশ্নে ভিন্ন ভিন্ন option-এ থাকতে হবে — কখনোই sequential pattern বা একই option বারবার না।
6. যত ধরনের সম্ভব MCQ variety বানাও — direct fact, definition, cause-effect, comparison, fill-in-the-blank style, "কোনটি সঠিক নয়" ধরনের প্রশ্ন — সব ধরনের প্রশ্ন mix করে বানাও, শুধু এক প্যাটার্নে আটকে থেকো না। প্রশ্ন বানানোর সময় বারবার টপিকের নাম ধরে ধরে প্রশ্ন শুরু করা যাবে না (যেমন "X সম্পর্কে কোনটি সঠিক", "X এর গঠন কী" — একই প্যাটার্নে বারবার) — বৈচিত্র্যপূর্ণ প্রশ্ন-গঠন ব্যবহার করবে, না হলে পড়তে boring লাগে।
7. প্রশ্ন text এর ভাষায় (বাংলা হলে বাংলা, ইংরেজি হলে ইংরেজি)
8. ৪টি option, একটি সঠিক (text থেকে সরাসরি), বাকি ৩টি distractor অবশ্যই text-এর অন্য অংশের প্রকৃত তথ্য/নাম/সংখ্যা থেকে নেওয়া (অন্য লাইনের সত্যিকার তথ্য এখানে ভুল option হিসেবে ব্যবহার করো) — সম্পূর্ণ কল্পনাপ্রসূত/বানানো distractor চলবে না।
8a. OPTION RELEVANCE RULE (must): ৪টি option একই category/type-এর হতে হবে যেমন প্রশ্নের answer-type — যদি প্রশ্নের উত্তর কোনো ব্যক্তি/বিজ্ঞানীর নাম হয়, ৪টি option-ই নাম হবে (এলোমেলো ভিন্ন-টাইপ জিনিস মেশানো যাবে না); যদি উত্তর percentage হয়, ৪টি option-ই percentage হবে; যদি উত্তর সংখ্যা/measurement হয়, ৪টি option-ই একই ধরনের সংখ্যা/measurement হবে। এভাবে option-গুলো দেখলেই বোঝা যাবে প্রশ্নটা আসলে কী নিয়ে, guessing কমে যাবে। বিশেষ ক্ষেত্রে text-এ সেই নির্দিষ্ট category-র পর্যাপ্ত রিলেটেড তথ্য না পাওয়া গেলে, topic-related কাছাকাছি extra info দিয়ে option বানানো যাবে (কিন্তু কখনো সম্পূর্ণ ভিন্ন ধরনের/অপ্রাসঙ্গিক option দেওয়া যাবে না)।
9. Explanation-এর মূল answer-confirm অংশ max 200 chars এর মধ্যে রাখার চেষ্টা করবে (rule 4b অনুযায়ী)।
10. কোনো section heading, "Card 1"/"Card 2", page/chapter label বা navigation text কোনো option হিসেবে ব্যবহার করা যাবে না — প্রতিটি option অবশ্যই actual factual content হতে হবে।
11. STRICTLY NISHIDDHO: প্রশ্নে বা option-এ কখনোই এই ধরনের কথা লেখা যাবে না — "টপিকের নাম কি", "এখানে কি বলা হয়েছে", "প্রদত্ত বর্ণনায় আছে যে", "পাঠ্যবস্তুটির টপিক", "উক্ত অনুচ্ছেদে/টেক্সটে উল্লেখিত", "...কী হিসেবে উল্লেখ করা হয়েছে", "...হিসেবে উল্লেখ করা হয়েছে", বা এই জাতীয় কোনো meta/source-reference কথা। প্রশ্ন সরাসরি বিষয়বস্তু নিয়ে হবে, যেন টেক্সট পড়ে না জানলেও প্রশ্নটা independent একটা knowledge question মনে হয়।
12. Text-এ থাকা যেকোনো #tag, @mention, © copyright line, channel/page/credit name, promotional line — এসব থেকে কোনো MCQ বানানো যাবে না এবং এসব কখনোই question বা option এর content হিসেবে ব্যবহার করা যাবে না।
13. QUESTION/OPTION LENGTH RULE: প্রশ্ন ছোট ও সরাসরি রাখবে (অতিরিক্ত বড় বাক্যের প্রশ্ন এড়িয়ে চলবে)। Option সাধারণত ছোট রাখবে, একটা MCQ-তে সর্বোচ্চ ১-২টা option তুলনামূলক বড় হতে পারে (যদি source fact নিজেই বড় হয়), কিন্তু পুরো ৪টা option বড় বা অতিরিক্ত জটিল বাক্য হওয়া যাবে না। অতিরিক্ত কঠিন/ঘোরানো-প্যাঁচানো বড় প্রশ্ন এড়িয়ে চলবে (rule 3a এর moderate-difficulty MCQ ছাড়া)।
14. MCQ সংখ্যা সাধারণত ১০-২৫টার মধ্যে রাখবে (text-এ তথ্য বেশি থাকলে সর্বোচ্চ ৩৫ পর্যন্ত যেতে পারে) — কিন্তু quantity-র চেয়ে quality সবসময় অগ্রাধিকার পাবে; কম fact থাকা text থেকে জোর করে বেশি MCQ বানানো যাবে না।

TEXT:
{text[:4000]}

Return ONLY valid JSON array, no markdown, no extra text:
[{{"question":"...","options":["...","...","...","..."],"answer":"B","explanation":"..."}}]"""

    def _parse_text_json(raw: str) -> list:
        raw = raw.strip()
        if "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()
        try:
            mcqs = _json.loads(raw)
        except Exception:
            return []
        return [m for m in mcqs if all(k in m for k in ["question","options","answer","explanation"])
                and len(m.get("options", [])) >= 4 and m["answer"] in ["A","B","C","D"]
                and not any(re.match(r'^(card|page|section|chapter|part|topic|slide)\s*\d*$', str(o).strip(), re.IGNORECASE) for o in m.get("options", []))]

    # ── PRIMARY: Gemini (new google.genai SDK, multi-key rotation) ──
    max_retries = len(key_rotator.keys) if key_rotator.keys else 3
    _ordered = key_rotator.ordered_keys(offset=_qbm_key_offset_ctx.get())
    _tried_keys = set()
    for attempt in range(max_retries):
        try:
            _fresh = key_rotator.ordered_keys(offset=_qbm_key_offset_ctx.get())
            _fresh = [k for k in _fresh if not _is_gemini_key_exhausted_today(k)] or _fresh
            _untried = [k for k in _fresh if k not in _tried_keys]
            if _untried:
                key = _untried[0]
            elif _fresh:
                key = _fresh[attempt % len(_fresh)]
            else:
                key = _ordered[attempt % len(_ordered)] if _ordered else key_rotator.get_key()
            _tried_keys.add(key)
            key_rotator.record_call(key)
            from google import genai as gai
            from google.genai import types
            # FIX (same class of bug as the image-path _call_gemini): no
            # client-level HTTP timeout meant asyncio.wait_for(timeout=45)
            # only stopped the outer await -- the background thread's real
            # request kept running on the SDK's own default, so a slow/dead
            # connection burned the full 45s every attempt instead of
            # failing fast into the next key.
            client = gai.Client(
                api_key=key,
                http_options=types.HttpOptions(timeout=38000)
            )

            def _call_gemini():
                return client.models.generate_content(
                    model="gemini-3.5-flash",
                    contents=[types.Part.from_text(text=prompt)],
                    config=types.GenerateContentConfig(max_output_tokens=8192)
                )

            async with key_rotator.throttled_call(key=key):
                response = await asyncio.wait_for(asyncio.to_thread(_call_gemini), timeout=45)
            valid = _parse_text_json(response.text)
            if valid:
                key_rotator.mark_healthy(key)
                logger.info(f"[Gemini-Text] {len(valid)} MCQs (attempt {attempt+1})")
                return valid
        except Exception as e:
            err_str = _gemini_full_error_text(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                is_daily = "PerDay" in err_str or "generate_content_free_tier_requests" in err_str
                key_rotator.mark_rate_limited(key, daily_exhausted=is_daily)
                logger.warning(f"[Gemini-Text] Attempt {attempt+1} rate-limited (429){' [daily quota]' if is_daily else ''}, cooling down: {e}")
            elif ("SUSPENDED" in err_str.upper() or "API_KEY_INVALID" in err_str.upper()
                  or "UNAUTHENTICATED" in err_str.upper() or "ACCOUNT_STATE_INVALID" in err_str.upper()
                  or "401" in err_str):
                logger.error(f"[Gemini-Text] Attempt {attempt+1}: key permanently banned (suspended/invalid): {e}")
                key_rotator.mark_banned(key, reason=err_str[:200]); key_rotator.record_account_error(key)
            else:
                logger.warning(f"[Gemini-Text] Attempt {attempt+1} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
            continue

    logger.warning("[Gemini-Text] All keys failed → trying OpenRouter text fallback")

    # ── FALLBACK: OpenRouter (text-only chat completion) ──
    if not openrouter_rotator.has_keys():
        logger.warning("[OpenRouter-Text] No keys available, skipping fallback")
        return []

    max_or_retries = len(openrouter_rotator.keys) * len(OPENROUTER_MODELS)
    for attempt in range(max(max_or_retries, 3)):
        model = OPENROUTER_MODELS[attempt % len(OPENROUTER_MODELS)]
        try:
            _ordered = openrouter_rotator.ordered_keys()
            key = _ordered[attempt % len(_ordered)] if _ordered else openrouter_rotator.get_key()
            async with httpx.AsyncClient(timeout=90) as client:
                r = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "HTTP-Referer": "https://atlascourses.com",
                        "X-Title": "ATLAS MCQ Bot"
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 4096
                    }
                )
            if r.status_code == 429:
                openrouter_rotator.mark_rate_limited(key)
                await asyncio.sleep(2)
                continue
            if r.status_code != 200:
                continue
            data = r.json()
            raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            valid = _parse_text_json(raw)
            if valid:
                openrouter_rotator.mark_healthy(key)
                logger.info(f"[OpenRouter-Text] {len(valid)} MCQs via {model}")
                return valid
        except Exception as e:
            logger.warning(f"[OpenRouter-Text] {model} attempt {attempt+1} failed: {e}")
            continue

    return []

# ============================================================
# PARSE HELPERS
# ============================================================
def parse_page_range(page_range: str) -> tuple:
    try:
        if "-" in page_range:
            parts = page_range.split("-")
            return int(parts[0]), int(parts[1])
        else:
            n = int(page_range)
            return n, n
    except:
        return None, None

def parse_pdf_command(text: str) -> dict:
    import re
    result = {
        "page_range": None,
        "channel_id": None,
        "topic": None,
        "mcq_count": None,
        "mcq_count_min": None,
        "mcq_count_max": None,
        "thread_id": None
    }
    try:
        p_match = re.search(r'-p\s+([\d\-]+)', text)
        if p_match:
            result["page_range"] = p_match.group(1)
        c_match = re.search(r'-c\s+(\S+)', text)
        if c_match:
            result["channel_id"] = c_match.group(1)
        # -t থ্রেড আইডি: কোটেশন সহ (-t "447") বা ছাড়া (-t 447) দুই ফরম্যাটেই কাজ করবে
        t_match = re.search(r'-t\s+"(\d+)"', text) or re.search(r"-t\s+'(\d+)'", text) or re.search(r'-t\s+(\d+)', text)
        if t_match:
            result["thread_id"] = int(t_match.group(1))
        m_match = re.search(r'-m\s+"([^"]+)"', text)
        if m_match:
            result["topic"] = m_match.group(1)
        else:
            m_match = re.search(r'-m\s+(\S+)', text)
            if m_match:
                result["topic"] = m_match.group(1)
        # [N-M] রেঞ্জ ব্র্যাকেট: প্রতি পেইজে MCQ সংখ্যা এই রেঞ্জের মধ্যে strictly
        # রাখতে হবে (min-max দুটোই মানতে হবে, কমও না বেশিও না)
        range_match = re.search(r'\[\.?(\d+)\s*-\s*(\d+)\.?\]', text)
        if range_match:
            lo, hi = int(range_match.group(1)), int(range_match.group(2))
            result["mcq_count_min"] = min(lo, hi)
            result["mcq_count_max"] = max(lo, hi)
        else:
            # [.N.] বা [N] ব্র্যাকেট: প্রতি পেইজে কতগুলো MCQ বানাতে হবে সেটা স্পষ্টভাবে
            # বোঝায় (কমান্ডের শেষে থাকা bare সংখ্যার অস্পষ্ট অনুমানের চেয়ে অগ্রাধিকার পাবে)
            bracket_match = re.search(r'\[\.?(\d+)\.?\]', text)
            if bracket_match:
                result["mcq_count"] = int(bracket_match.group(1))
            else:
                cmd_part = text.split('/pdf')[1] if '/pdf' in text else text
                nums = re.findall(r'(?<!\d)(\d+)(?!\d)', cmd_part)
                if nums:
                    last_num = int(nums[-1])
                    page_nums = result["page_range"].replace("-", " ").split() if result["page_range"] else []
                    if str(last_num) not in page_nums and last_num < 200:
                        result["mcq_count"] = last_num
    except Exception as e:
        logger.error(f"[Parse] PDF command error: {e}")
    return result

def fmt_page(n: int) -> str:
    return str(n).zfill(2)

def gen_session_id() -> str:
    import random, string
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

# ============================================================
# ISLAMIC AYATS + MOTIVATION
# ============================================================
ISLAMIC_AYATS = [
    '"নিশ্চয়ই কষ্টের সাথে স্বস্তি আছে।" (সূরা ইনশিরাহ: ৬)',
    '"আল্লাহ কোনো আত্মার উপর তার সাধ্যের বাইরে বোঝা চাপান না।" (সূরা বাকারা: ২৮৬)',
    '"জ্ঞানীরাই আল্লাহকে বেশি ভয় করে।" (সূরা ফাতির: ২৮)',
    '"তোমরা হতাশ হয়ো না, দুঃখ করো না। তোমরাই বিজয়ী হবে।" (সূরা আল-ইমরান: ১৩৯)',
    '"আল্লাহর রহমত থেকে নিরাশ হয়ো না।" (সূরা যুমার: ৫৩)',
    '"সবর করো, নিশ্চয়ই আল্লাহ সবরকারীদের সাথে আছেন।" (সূরা বাকারা: ১৫৩)',
    '"তোমাদের প্রতিপালক বলেন: আমাকে ডাকো, আমি সাড়া দেব।" (সূরা মুমিন: ৬০)',
    '"যে আল্লাহর উপর ভরসা করে, তার জন্য আল্লাহই যথেষ্ট।" (সূরা তালাক: ৩)',
    '"আল্লাহ তাওবাকারীদের ভালোবাসেন।" (সূরা বাকারা: ২২২)',
    '"প্রতিটি কঠিনতার সাথেই সহজতা রয়েছে।" (সূরা ইনশিরাহ: ৫)',
]

def get_random_ayat() -> str:
    return random.choice(ISLAMIC_AYATS)

def get_motivation(pct: float) -> str:
    if pct >= 90:
        return "🏆 অসাধারণ! তুমি সেরা! আরও এগিয়ে যাও!"
    elif pct >= 70:
        return "🎉 চমৎকার! তুমি খুব ভালো করেছো!"
    elif pct >= 50:
        return "👍 মোটামুটি ভালো! আরও একটু পড়াশোনা করো!"
    else:
        return "📚 পড়া হয়নি! আবার পড়ে চেষ্টা করো!"

generate_new_mcq = generate_mcq_from_image
