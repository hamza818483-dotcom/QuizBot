# ============================================================
# lms_live_quiz.py
# LMS send-icon "Live Quiz" mode — instant or scheduled.
# MCQ source = LMS's own exam_questions table (question bank), not a
# CSV upload. Reuses app.py's existing start_live_quiz() runner (the
# same one /live uses) so the on-channel behavior (poll → stopPoll →
# reveal → next, real-time scoring, grand result) is identical.
#
# LMS Supabase (atlascourses.com project) is a DIFFERENT Supabase
# project from QuizBot's own — accessed here via REST using the
# existing LMS_SUPABASE_URL / LMS_SUPABASE_SERVICE_KEY env vars
# (same ones lms_upload.py already uses).
# ============================================================
import logging
import time
from typing import Optional

import httpx

from core import LMS_SUPABASE_URL, LMS_SUPABASE_SERVICE_KEY

logger = logging.getLogger("atlas.lms_live_quiz")

ANS_MAP = {"1": "A", "2": "B", "3": "C", "4": "D", "A": "A", "B": "B", "C": "C", "D": "D"}


def _headers():
    return {
        "apikey": LMS_SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {LMS_SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


class LmsLiveQuizError(Exception):
    pass


async def fetch_exam_mcqs(exam_id: str) -> tuple[list, str]:
    """Returns (mcqs, exam_title). mcqs shape matches start_live_quiz's
    expected format: {"question","options"[4],"answer","explanation"}."""
    if not LMS_SUPABASE_URL or not LMS_SUPABASE_SERVICE_KEY:
        raise LmsLiveQuizError("LMS_SUPABASE_URL / LMS_SUPABASE_SERVICE_KEY env var সেট করা নেই।")
    async with httpx.AsyncClient(timeout=30) as client:
        exam_r = await client.get(
            f"{LMS_SUPABASE_URL}/rest/v1/exams",
            headers=_headers(),
            params={"id": f"eq.{exam_id}", "select": "id,title", "limit": "1"},
        )
        exam_r.raise_for_status()
        exam_rows = exam_r.json()
        if not exam_rows:
            raise LmsLiveQuizError("Exam পাওয়া যায়নি।")
        exam_title = exam_rows[0].get("title") or "ATLAS Live Quiz"

        q_r = await client.get(
            f"{LMS_SUPABASE_URL}/rest/v1/exam_questions",
            headers=_headers(),
            params={
                "exam_id": f"eq.{exam_id}",
                "select": "question_text,option_a,option_b,option_c,option_d,correct_option,explanation,question_index",
                "order": "question_index.asc",
            },
        )
        q_r.raise_for_status()
        rows = q_r.json()

    mcqs = []
    for row in rows:
        opts = [row.get("option_a") or "", row.get("option_b") or "", row.get("option_c") or "", row.get("option_d") or ""]
        opts = [o for o in opts if o.strip()]
        if len(opts) < 2:
            continue
        ans = ANS_MAP.get(str(row.get("correct_option") or "A").strip().upper(), "A")
        mcqs.append({
            "question": row.get("question_text") or "",
            "options": opts,
            "answer": ans,
            "explanation": row.get("explanation") or "",
        })
    return mcqs, exam_title


async def create_scheduled_live_quiz(
    name: str, exam_id: str, chat_id: str, thread_id: Optional[int],
    per_q_time_sec: int, scheduled_at_iso: str, channel_row_id: Optional[str] = None,
) -> dict:
    """Insert a row into LMS's scheduled_live_quizzes table. Validates the
    exam has questions up front so a typo'd exam_id fails fast instead of
    silently sitting pending forever."""
    mcqs, _ = await fetch_exam_mcqs(exam_id)
    if not mcqs:
        raise LmsLiveQuizError("এই exam-এ কোনো প্রশ্ন নেই।")

    payload = {
        "name": name.strip() or "ATLAS Live Quiz",
        "exam_id": exam_id,
        "channel_id": channel_row_id,
        "chat_id": str(chat_id).strip(),
        "thread_id": thread_id,
        "per_q_time_sec": max(5, int(per_q_time_sec or 20)),
        "scheduled_at": scheduled_at_iso,
        "status": "pending",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{LMS_SUPABASE_URL}/rest/v1/scheduled_live_quizzes",
            headers=_headers(),
            json=payload,
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else payload


async def list_due_live_quizzes() -> list:
    """Rows with status=pending and scheduled_at <= now. Used by both the
    instant-send path (scheduled_at = now) and the background cron."""
    if not LMS_SUPABASE_URL or not LMS_SUPABASE_SERVICE_KEY:
        return []
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{LMS_SUPABASE_URL}/rest/v1/scheduled_live_quizzes",
            headers=_headers(),
            params={
                "status": "eq.pending",
                "scheduled_at": f"lte.{now_iso}",
                "select": "*",
                "order": "scheduled_at.asc",
            },
        )
        r.raise_for_status()
        return r.json()


async def claim_live_quiz(row_id: str, job_session_id: str) -> bool:
    """Atomically flips a row from pending -> running (filtered PATCH: only
    matches if it's STILL pending). Returns True iff this call won the
    claim — guards against two overlapping scheduler ticks firing the same
    row twice."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.patch(
            f"{LMS_SUPABASE_URL}/rest/v1/scheduled_live_quizzes",
            headers=_headers(),
            params={"id": f"eq.{row_id}", "status": "eq.pending"},
            json={
                "status": "running",
                "job_session_id": job_session_id,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            },
        )
        r.raise_for_status()
        return len(r.json()) > 0


async def mark_live_quiz(row_id: str, status: str, error: str = None, job_session_id: str = None):
    patch = {"status": status}
    if error is not None:
        patch["error"] = error[:500]
    if job_session_id is not None:
        patch["job_session_id"] = job_session_id
    if status == "running":
        patch["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    if status in ("done", "error", "cancelled"):
        patch["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            await client.patch(
                f"{LMS_SUPABASE_URL}/rest/v1/scheduled_live_quizzes",
                headers=_headers(),
                params={"id": f"eq.{row_id}"},
                json=patch,
            )
        except Exception as e:
            logger.warning(f"[LMS-LiveQuiz] status patch failed for {row_id}: {e}")
