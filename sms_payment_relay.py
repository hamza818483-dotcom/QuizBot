# ============================================================
# sms_payment_relay.py
# Receives parsed bKash/Nagad SMS (via Macrodroid on the admin's phone),
# matches it to a pending payment_requests row, and auto-approves via the
# existing approve_payment_request() RPC (same one the admin panel button
# calls — we don't reimplement enrollment/notification logic here).
#
# Matching is amount + sender phone number ONLY. No trx_id anywhere in
# this flow — students are never asked for it (most don't understand what
# it is), so it is not collected, not parsed, not stored, not matched on.
# Students enter two things on the existing payment form: how much they
# sent, and the number they sent it from (sender_last5) — those two are
# the only match keys here, matching what the form already asks for.
#
# LMS Supabase (atlascourses.com project) is accessed the same way
# lms_upload.py / lms_live_quiz.py already do: REST, via
# LMS_SUPABASE_URL / LMS_SUPABASE_SERVICE_KEY.
# ============================================================
import logging
import re
import time
from typing import Optional

import httpx

from core import LMS_SUPABASE_URL, LMS_SUPABASE_SERVICE_KEY

logger = logging.getLogger("atlas.sms_payment_relay")

# Only amount + sender number are extracted from the SMS text — no trx_id.
_AMOUNT_RE = re.compile(r"Tk\.?\s*([0-9][0-9,]*\.?[0-9]*)", re.IGNORECASE)
_SENDER_RE = re.compile(r"(?:from|Sender:?)\s*(01[0-9]{9})", re.IGNORECASE)
# A payment-received SMS always has one of these verbs near the amount;
# this keeps OTPs/promos/balance-check SMS from being treated as payments.
_PAYMENT_HINT_RE = re.compile(r"received|cash in", re.IGNORECASE)


def parse_sms_text(body: str):
    """Returns (amount, sender_phone) or (None, None) if this doesn't look
    like a bKash/Nagad payment-received SMS."""
    if not body or not _PAYMENT_HINT_RE.search(body):
        return None, None
    amount_match = _AMOUNT_RE.search(body)
    if not amount_match:
        return None, None
    amount = amount_match.group(1).replace(",", "")
    sender_match = _SENDER_RE.search(body)
    sender_phone = sender_match.group(1) if sender_match else None
    return amount, sender_phone


def _headers():
    return {
        "apikey": LMS_SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {LMS_SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def _log_row(client: httpx.AsyncClient, amount, sender_phone, raw_sms,
                    received_at, status: str, matched_id: Optional[str] = None, note: Optional[str] = None):
    try:
        await client.post(
            f"{LMS_SUPABASE_URL}/rest/v1/sms_payment_relay_log",
            headers=_headers(),
            json={
                "amount": amount,
                "sender_phone": sender_phone,
                "raw_sms": raw_sms,
                "received_at": received_at,
                "status": status,
                "matched_payment_request_id": matched_id,
                "note": note,
            },
        )
    except Exception as e:
        logger.warning(f"[SMS-Relay] failed to write log row: {e}")


async def process_incoming_sms(amount: Optional[str], sender_phone: Optional[str],
                                raw_sms: Optional[str], received_at_ms: Optional[int]) -> dict:
    """Core logic: find a matching pending payment_requests row (by amount
    + sender_last5 only) and approve it. Returns a dict describing what
    happened (for the API response)."""
    if not LMS_SUPABASE_URL or not LMS_SUPABASE_SERVICE_KEY:
        raise RuntimeError("LMS_SUPABASE_URL / LMS_SUPABASE_SERVICE_KEY env var সেট করা নেই।")
    if not amount or not sender_phone:
        return {"matched": False, "reason": "amount or sender phone missing from SMS"}

    received_at_iso = None
    if received_at_ms:
        try:
            received_at_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(received_at_ms / 1000))
        except Exception:
            pass

    sender_last5 = sender_phone[-5:]
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 48 * 3600))

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{LMS_SUPABASE_URL}/rest/v1/payment_requests",
            headers=_headers(),
            params={
                "status": "eq.pending",
                "amount_sent": f"eq.{amount}",
                "sender_last5": f"eq.{sender_last5}",
                "created_at": f"gte.{cutoff}",
                "select": "id,amount_sent,status,sender_last5",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        r.raise_for_status()
        rows = r.json()

        if not rows:
            await _log_row(client, amount, sender_phone, raw_sms, received_at_iso,
                            status="unmatched", note="কোনো pending payment_requests row মিলেনি (amount+number)")
            return {"matched": False, "reason": "no matching pending payment request"}

        request_id = rows[0]["id"]

        # Call the existing RPC — same path the admin "Approve" button
        # uses, so enrollment / email-confirm bypass / notifications all
        # happen exactly the same way as a manual approval.
        rpc_r = await client.post(
            f"{LMS_SUPABASE_URL}/rest/v1/rpc/approve_payment_request",
            headers=_headers(),
            json={"p_request_id": request_id},
        )
        if rpc_r.status_code >= 400:
            await _log_row(client, amount, sender_phone, raw_sms, received_at_iso,
                            status="error", matched_id=request_id, note=f"RPC failed: {rpc_r.text[:300]}")
            return {"matched": True, "approved": False, "request_id": request_id, "error": rpc_r.text[:300]}

        await _log_row(client, amount, sender_phone, raw_sms, received_at_iso,
                        status="matched", matched_id=request_id, note="Auto-approved (amount+number)")
        logger.info(f"[SMS-Relay] Approved payment_request {request_id} via amount+number match")
        return {"matched": True, "approved": True, "request_id": request_id}
