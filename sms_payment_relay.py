# ============================================================
# sms_payment_relay.py
# Receives parsed bKash/Nagad SMS from the ATLAS SMS Relay Android app
# (android-app/atlas-sms-relay in the LMS repo), matches it to a pending
# payment_requests row, and auto-approves via the existing
# approve_payment_request() RPC (same one the admin panel button calls —
# we don't reimplement enrollment/notification logic here).
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

# Same parsing rules as the Android app's SmsParser.java (kept here too so
# Macrodroid — which just forwards the raw SMS text, no parsing of its
# own — can be used instead of the custom Android app).
_TRX_ID_RE = re.compile(r"TrxID\s*[:\-]?\s*([A-Za-z0-9]{6,})", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"Tk\.?\s*([0-9][0-9,]*\.?[0-9]*)", re.IGNORECASE)
_SENDER_RE = re.compile(r"from\s+(01[0-9]{9})", re.IGNORECASE)


def parse_sms_text(body: str):
    """Returns (trx_id, amount, sender_phone) or (None, None, None) if this
    doesn't look like a bKash/Nagad payment-received SMS."""
    if not body:
        return None, None, None
    trx_match = _TRX_ID_RE.search(body)
    if not trx_match:
        return None, None, None
    amount_match = _AMOUNT_RE.search(body)
    if not amount_match:
        return None, None, None
    trx_id = trx_match.group(1)
    amount = amount_match.group(1).replace(",", "")
    sender_match = _SENDER_RE.search(body)
    sender_phone = sender_match.group(1) if sender_match else None
    return trx_id, amount, sender_phone


def _headers():
    return {
        "apikey": LMS_SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {LMS_SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def _log_row(client: httpx.AsyncClient, trx_id: str, amount, sender_phone, raw_sms,
                    received_at, status: str, matched_id: Optional[str] = None, note: Optional[str] = None):
    try:
        await client.post(
            f"{LMS_SUPABASE_URL}/rest/v1/sms_payment_relay_log",
            headers=_headers(),
            json={
                "trx_id": trx_id,
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


async def process_incoming_sms(trx_id: str, amount: Optional[str], sender_phone: Optional[str],
                                raw_sms: Optional[str], received_at_ms: Optional[int]) -> dict:
    """Core logic: find a matching pending payment_requests row and approve
    it. Returns a dict describing what happened (for the API response).

    Matching strategy (trx_id is NOT required from students — most don't
    understand what it is, so we never ask for it; it's only used here if
    a payment_requests row happens to have one saved from an earlier,
    unrelated flow):
      1) amount + sender_last5 (last 5 digits of the paying number, which
         students DO enter on the payment form) — strong, unambiguous match
      2) amount + trx_id, if a row already has trx_id set for some reason
      3) amount alone, ONLY if exactly one pending request has that amount
         in the last 48h — ambiguous cases (two students paying the same
         amount around the same time) are deliberately left unmatched for
         manual admin review rather than risk approving the wrong student.
    """
    if not LMS_SUPABASE_URL or not LMS_SUPABASE_SERVICE_KEY:
        raise RuntimeError("LMS_SUPABASE_URL / LMS_SUPABASE_SERVICE_KEY env var সেট করা নেই।")

    received_at_iso = None
    if received_at_ms:
        try:
            received_at_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(received_at_ms / 1000))
        except Exception:
            pass

    sender_last5 = (sender_phone or "")[-5:] if sender_phone else None
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 48 * 3600))

    async with httpx.AsyncClient(timeout=20) as client:
        rows = []

        # 1) Strongest match: amount + sender_last5 (what students actually
        #    enter on the payment form).
        if amount and sender_last5:
            r1 = await client.get(
                f"{LMS_SUPABASE_URL}/rest/v1/payment_requests",
                headers=_headers(),
                params={
                    "status": "eq.pending",
                    "amount_sent": f"eq.{amount}",
                    "sender_last5": f"eq.{sender_last5}",
                    "created_at": f"gte.{cutoff}",
                    "select": "id,trx_id,amount_sent,status,sender_last5",
                    "order": "created_at.desc",
                    "limit": "1",
                },
            )
            r1.raise_for_status()
            rows = r1.json()

        # 2) If this payment_requests row happens to already have a trx_id
        #    saved (e.g. from a different submission flow), match on that.
        if not rows and trx_id:
            r2 = await client.get(
                f"{LMS_SUPABASE_URL}/rest/v1/payment_requests",
                headers=_headers(),
                params={
                    "trx_id": f"eq.{trx_id}",
                    "status": "eq.pending",
                    "select": "id,trx_id,amount_sent,status",
                    "limit": "1",
                },
            )
            r2.raise_for_status()
            rows = r2.json()

        # 3) Last resort: amount alone, only if unambiguous (exactly one
        #    pending candidate) — see docstring above for why.
        if not rows and amount:
            r3 = await client.get(
                f"{LMS_SUPABASE_URL}/rest/v1/payment_requests",
                headers=_headers(),
                params={
                    "status": "eq.pending",
                    "amount_sent": f"eq.{amount}",
                    "created_at": f"gte.{cutoff}",
                    "select": "id,trx_id,amount_sent,status,sender_last5",
                    "order": "created_at.desc",
                    "limit": "5",
                },
            )
            r3.raise_for_status()
            candidates = r3.json()
            if len(candidates) == 1:
                rows = candidates

        if not rows:
            await _log_row(client, trx_id, amount, sender_phone, raw_sms, received_at_iso,
                            status="unmatched", note="কোনো pending payment_requests row মিলেনি")
            return {"matched": False, "reason": "no matching pending payment request"}

        request_id = rows[0]["id"]

        # 3) Call the existing RPC — same path the admin "Approve" button
        #    uses, so enrollment / email-confirm bypass / notifications all
        #    happen exactly the same way as a manual approval.
        rpc_r = await client.post(
            f"{LMS_SUPABASE_URL}/rest/v1/rpc/approve_payment_request",
            headers=_headers(),
            json={"p_request_id": request_id},
        )
        if rpc_r.status_code >= 400:
            await _log_row(client, trx_id, amount, sender_phone, raw_sms, received_at_iso,
                            status="error", matched_id=request_id, note=f"RPC failed: {rpc_r.text[:300]}")
            return {"matched": True, "approved": False, "request_id": request_id, "error": rpc_r.text[:300]}

        # Also store the trx_id on the row if it wasn't already set (the
        # amount-fallback path above matches rows with no trx_id yet).
        await client.patch(
            f"{LMS_SUPABASE_URL}/rest/v1/payment_requests",
            headers=_headers(),
            params={"id": f"eq.{request_id}"},
            json={"trx_id": trx_id},
        )

        await _log_row(client, trx_id, amount, sender_phone, raw_sms, received_at_iso,
                        status="matched", matched_id=request_id, note="Auto-approved")
        logger.info(f"[SMS-Relay] Approved payment_request {request_id} via trx {trx_id}")
        return {"matched": True, "approved": True, "request_id": request_id}
