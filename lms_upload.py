# ============================================================
# lms_upload.py
# /autolms কমান্ডের জন্য — chorcha.net থেকে scrape করা MCQ সরাসরি
# LMS (atlascourses.com)-এর Supabase-এ exam হিসেবে insert করে।
# LMS নিজেই Supabase থেকে read করে দেখায়, তাই এখানে insert হলেই
# ওয়েবসাইটে অটো চলে আসে — আলাদা কোনো "upload" step লাগে না।
# ============================================================
import logging
import re

import httpx

from core import LMS_SUPABASE_URL, LMS_SUPABASE_SERVICE_KEY

logger = logging.getLogger("atlas.lms_upload")


class LmsUploadError(Exception):
    pass


ANS_MAP = {"1": "A", "2": "B", "3": "C", "4": "D", "A": "A", "B": "B", "C": "C", "D": "D"}


def _headers():
    return {
        "apikey": LMS_SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {LMS_SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def _title_exists(client: httpx.AsyncClient, title: str) -> bool:
    r = await client.get(
        f"{LMS_SUPABASE_URL}/rest/v1/exams",
        headers=_headers(),
        params={"title": f"eq.{title}", "select": "id", "limit": "1"},
    )
    r.raise_for_status()
    return len(r.json()) > 0


async def upload_mcqs_to_lms(
    results: list,
    title: str,
    subject: str,
    chapter: str,
    course_id: str | None = None,
    readymade_topic: str | None = None,
    readymade_sub_chapter: str | None = None,
    readymade_category: str | None = None,
    duration_minutes: int | None = None,
    total_marks: float | None = None,
    negative_mark_per_question: float | None = None,
    instructions: str | None = None,
    time_window_start: str | None = None,
    is_visible_on_free: bool = False,
    is_published: bool = True,
    restrict_solution: bool = False,
    disable_second_timer_deduction: bool = False,
    is_only_live: bool = False,
) -> dict:
    """
    results: list of dicts shaped like chorcha_parser/atlas_mhtml output --
             {"questions","option1..4","option5","answer","explanation","type","section"}
    title: exam title (chapter/topic name as given in the /autolms command)
    subject, chapter: from user-provided command args (also mirrored onto
                       readymade_topic/readymade_sub_chapter if those aren't
                       given separately -- covers the common case where
                       they're the same value).
    course_id: optional -- leave None for global Readymade (not tied to one course)
    duration_minutes: defaults to len(results) minutes if not given
    total_marks: defaults to len(results) if not given
    is_published: defaults True so it's visible immediately (matches how
                   a manually-created Readymade exam behaves by default)

    Returns {"exam_id": ..., "title": ..., "count": N}
    Raises LmsUploadError on any failure -- no partial state is left behind
    (if question insert fails, the just-created exam row is deleted).
    """
    if not LMS_SUPABASE_URL or not LMS_SUPABASE_SERVICE_KEY:
        raise LmsUploadError(
            "LMS_SUPABASE_URL / LMS_SUPABASE_SERVICE_KEY env var সেট করা নেই।"
        )
    if not results:
        raise LmsUploadError("কোনো MCQ নেই, আপলোড করার কিছু নেই।")

    n = len(results)
    async with httpx.AsyncClient(timeout=30) as client:
        final_title = title
        if await _title_exists(client, title):
            final_title = f"{title} (New)"
            # extremely unlikely, but guard against "(New)" itself colliding
            k = 2
            while await _title_exists(client, final_title):
                final_title = f"{title} (New {k})"
                k += 1

        exam_payload = {
            "title": final_title,
            "is_readymade": True,
            "exam_type": "practice",
            "duration_minutes": duration_minutes if duration_minutes is not None else max(n, 1),
            "subject": [subject] if subject else [],
            "chapter": chapter or None,
            "readymade_topic": readymade_topic or subject or None,
            "readymade_sub_chapter": readymade_sub_chapter or chapter or None,
            "readymade_category": readymade_category or None,
            "category": [],
            "is_published": is_published,
            "is_visible_on_free": is_visible_on_free,
            "restrict_solution": restrict_solution,
            "disable_second_timer_deduction": disable_second_timer_deduction,
            "is_only_live": is_only_live,
        }
        if total_marks is not None:
            exam_payload["total_marks"] = total_marks
        if negative_mark_per_question is not None:
            exam_payload["negative_mark_per_question"] = negative_mark_per_question
        if instructions:
            exam_payload["instructions"] = instructions
        if time_window_start:
            exam_payload["time_window_start"] = time_window_start
            # Readymade exams: no end-time limit once a start time is set --
            # leave time_window_end unset (NULL) so access never expires.
        if course_id:
            exam_payload["course_id"] = course_id

        r = await client.post(
            f"{LMS_SUPABASE_URL}/rest/v1/exams", headers=_headers(), json=exam_payload
        )
        if r.status_code not in (200, 201):
            raise LmsUploadError(f"Exam create ব্যর্থ: {r.status_code} {r.text[:300]}")
        exam_row = r.json()[0]
        exam_id = exam_row["id"]

        try:
            question_rows = []
            for idx, row in enumerate(results):
                ans_raw = str(row.get("answer", "")).strip()
                correct = ANS_MAP.get(ans_raw, "A")
                question_rows.append({
                    "exam_id": exam_id,
                    "question_index": idx,
                    "question_text": row.get("questions", ""),
                    "option_a": row.get("option1", "") or "",
                    "option_b": row.get("option2", "") or "",
                    "option_c": row.get("option3", "") or "",
                    "option_d": row.get("option4", "") or "",
                    "option_e": row.get("option5") or None,
                    "correct_option": correct,
                    "marks": 1,
                    "explanation": row.get("explanation") or None,
                    "question_type": str(row.get("type")) if row.get("type") is not None else None,
                    "section": str(row.get("section")) if row.get("section") is not None else None,
                    "subject": subject or None,
                    "chapter": chapter or None,
                })

            # Supabase REST insert in batches (avoid overly large single payload)
            BATCH = 200
            for i in range(0, len(question_rows), BATCH):
                batch = question_rows[i:i + BATCH]
                rq = await client.post(
                    f"{LMS_SUPABASE_URL}/rest/v1/exam_questions",
                    headers=_headers(),
                    json=batch,
                )
                if rq.status_code not in (200, 201):
                    raise LmsUploadError(
                        f"Question insert ব্যর্থ (batch {i}): {rq.status_code} {rq.text[:300]}"
                    )
        except Exception:
            # Rollback: delete the exam row so we never leave a title with 0 questions
            try:
                await client.delete(
                    f"{LMS_SUPABASE_URL}/rest/v1/exams",
                    headers=_headers(),
                    params={"id": f"eq.{exam_id}"},
                )
            except Exception as cleanup_err:
                logger.error(f"[lms_upload] rollback delete also failed for exam {exam_id}: {cleanup_err}")
            raise

        logger.info(f"[lms_upload] exam created: {final_title} ({len(question_rows)} MCQs, id={exam_id})")
        return {"exam_id": exam_id, "title": final_title, "count": len(question_rows)}
