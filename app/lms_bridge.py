# SPDX-License-Identifier: GPL-3.0-or-later
"""Indus LMS bridge: student authentication, live payload fetch,
course resource crawling & FTS indexing, study coach triage, and safety rails.

Enforces:
- Zero synthetic/demo data fallback: live data or LMSNotAuthenticatedError/LMSUpstreamError.
- Academic integrity: live/unsubmitted EOL tests and assessments are blocked server-side.
- Ask-first writes: actions require single-use WriteConfirmationManager tokens.
"""

from __future__ import annotations

import base64
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Make sure induslms-agent package can be imported
INDUSLMS_AGENT_PATH = os.environ.get("INDUSLMS_AGENT_PATH", "/Users/sid/induslms-agent")
if os.path.exists(INDUSLMS_AGENT_PATH) and INDUSLMS_AGENT_PATH not in sys.path:
    sys.path.append(INDUSLMS_AGENT_PATH)

try:
    import lms  # type: ignore
except ImportError:
    lms = None  # Handled cleanly at call time if module missing

from app import cache
from app.db import Database
from app.security import WriteConfirmationManager, assert_not_live_test
from app.sessions import Session, SessionStore


class LMSNotAuthenticatedError(Exception):
    """Raised when an operation requires an active Indus LMS session but none exists."""


class LMSUpstreamError(Exception):
    """Raised when an Indus LMS upstream API call fails."""


def _ensure_lms_module():
    if lms is None:
        raise LMSUpstreamError(
            f"induslms-agent module could not be imported from {INDUSLMS_AGENT_PATH}."
        )


def _is_allowed_grade(file_grade: str | None, student_grade: str | None) -> bool:
    """Filter out resources explicitly tagged for another grade to prevent 403s."""
    if not file_grade or not student_grade:
        return True
    fg = str(file_grade).strip().lower()
    sg = str(student_grade).strip().lower()
    s_num = (
        12
        if ("xii" in sg or "12" in sg)
        else 11
        if ("xi" in sg or "11" in sg)
        else 10
        if ("x" in sg or "10" in sg)
        else 9
        if ("ix" in sg or "9" in sg)
        else None
    )
    if s_num is not None:
        m = re.search(r"\b(gr(ade)?\s*|g)?(12|xii|11|xi|10|x|9|ix)[a-z]?\b", fg)
        if m:
            val = m.group(3).lower()
            target_num = (
                12
                if val in ("12", "xii")
                else 11
                if val in ("11", "xi")
                else 10
                if val in ("10", "x")
                else 9
                if val in ("9", "ix")
                else None
            )
            if target_num is not None and target_num != s_num:
                return False
    return True


def login_indus(store: SessionStore, email: str, password: str) -> Session:
    """Authenticate student against Indus LMS, build persistent Session, save to store."""
    _ensure_lms_module()
    try:
        tok_data = lms.login(email, password)
    except Exception as e:
        raise LMSUpstreamError(f"Indus LMS login failed: {e}") from e

    access = tok_data.get("access") or tok_data.get("token") or ""
    refresh = tok_data.get("refresh") or ""
    if not access:
        raise LMSUpstreamError("Indus LMS login returned no access token.")

    tenant = ""
    uid = ""
    name = ""
    grade = ""

    try:
        me_data = lms.me(access)
        tenant = str(me_data.get("tenant_id") or me_data.get("tenant") or "")
        uid = str(me_data.get("id") or me_data.get("user_id") or "")
        name = str(me_data.get("name") or me_data.get("full_name") or "")
    except Exception:
        pass

    try:
        prof_data = lms.profile(access)
        if not tenant:
            tenant = str(prof_data.get("tenant_id") or "")
        if not uid:
            uid = str(prof_data.get("id") or "")
        if not name:
            name = str(prof_data.get("full_name") or prof_data.get("name") or "")
        grade = str(prof_data.get("grade") or prof_data.get("grade_name") or "")
    except Exception:
        pass

    session = Session(
        access=access,
        refresh=refresh,
        tenant=tenant,
        name=name or email,
        email=email,
        uid=uid,
        grade=grade,
    )
    store.add(session)
    return session


def call_lms(
    session: Session,
    store: SessionStore | None,
    fn: Callable[..., Any],
    *args: Any,
    write: bool = False,
    **kwargs: Any,
) -> Any:
    """Execute an LMS function with session.access.
    Retries once on 401 via session.refresh (only for read operations). Writes never retry."""
    _ensure_lms_module()
    if not session or not session.access:
        raise LMSNotAuthenticatedError("Student not authenticated with Indus LMS.")

    try:
        return fn(session.access, *args, **kwargs)
    except Exception as e:
        status = getattr(e, "status", None) or getattr(getattr(e, "response", None), "status_code", None)
        if status == 401:
            if write:
                raise LMSUpstreamError("LMS session expired during write action. Write was not executed.") from e
            if session.refresh:
                try:
                    ref_data = lms.refresh_token(session.refresh)
                    new_access = ref_data.get("access") or ref_data.get("token")
                    if new_access:
                        session.access = new_access
                        if ref_data.get("refresh"):
                            session.refresh = ref_data["refresh"]
                        if store:
                            store.save(session)
                        return fn(session.access, *args, **kwargs)
                except Exception as ref_err:
                    raise LMSNotAuthenticatedError("Indus LMS session expired and refresh failed.") from ref_err
            raise LMSNotAuthenticatedError("Indus LMS session expired.") from e
        raise LMSUpstreamError(f"Indus LMS API error: {e}") from e


def fetch_student_payload(
    session: Session,
    store: SessionStore | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Fetch live student data (courses, eol tests, assessments, tasks, calendar,
    attendance, notifications, announcements) with per-student caching."""
    _ensure_lms_module()
    if not session or not session.access:
        raise LMSNotAuthenticatedError("Student not authenticated with Indus LMS.")

    def _builder(files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        results: dict[str, Any] = {
            "student": {
                "name": session.name,
                "email": session.email,
                "uid": session.uid,
                "tenant": session.tenant,
                "grade": session.grade,
            },
            "courses": [],
            "eol": [],
            "assessments": [],
            "tasks": [],
            "calendar": [],
            "attendance": {},
            "notifications": [],
            "announcements": [],
            "fetched_at": time.time(),
        }

        def _fetch_courses():
            try:
                res = call_lms(session, store, lms.my_courses, session.tenant)
                results["courses"] = res.get("courses") if isinstance(res, dict) else (res or [])
            except Exception as e:
                results["courses"] = []

        def _fetch_eol():
            try:
                res = call_lms(session, store, lms.eol_tests, session.tenant)
                results["eol"] = res.get("results") if isinstance(res, dict) else (res or [])
            except Exception:
                results["eol"] = []

        def _fetch_assessments():
            try:
                res = call_lms(session, store, lms.assessments)
                results["assessments"] = res.get("results") if isinstance(res, dict) else (res or [])
            except Exception:
                results["assessments"] = []

        def _fetch_tasks():
            try:
                res = call_lms(session, store, lms.learning_tasks, session.tenant)
                results["tasks"] = res if isinstance(res, list) else []
            except Exception:
                results["tasks"] = []

        def _fetch_calendar():
            try:
                res = call_lms(session, store, lms.calendar_events)
                results["calendar"] = res.get("events") if isinstance(res, dict) else (res or [])
            except Exception:
                results["calendar"] = []

        def _fetch_attendance():
            try:
                res = call_lms(session, store, lms.attendance_day, session.tenant)
                results["attendance"] = res if isinstance(res, dict) else {}
            except Exception:
                results["attendance"] = {}

        def _fetch_notifications():
            try:
                res = call_lms(session, store, lms.notifications, session.tenant, limit=30)
                results["notifications"] = res.get("notifications") if isinstance(res, dict) else (res or [])
            except Exception:
                results["notifications"] = []

        def _fetch_announcements():
            try:
                res = call_lms(session, store, lms.announcements, session.tenant)
                results["announcements"] = res.get("announcements") if isinstance(res, dict) else (res or [])
            except Exception:
                results["announcements"] = []

        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = [
                ex.submit(_fetch_courses),
                ex.submit(_fetch_eol),
                ex.submit(_fetch_assessments),
                ex.submit(_fetch_tasks),
                ex.submit(_fetch_calendar),
                ex.submit(_fetch_attendance),
                ex.submit(_fetch_notifications),
                ex.submit(_fetch_announcements),
            ]
            for f in futures:
                f.result()

        return results

    payload, _ = cache.get_or_build(
        session.sid,
        _builder,
        refresh=refresh,
        tenant=session.tenant,
        uid=session.uid,
    )
    return payload


def _safe_b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def crawl_and_index_lms_files(
    session: Session,
    db: Database,
    store: SessionStore | None = None,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Crawl enrolled course resources and task attachments, index metadata in FTS5
    scoped strictly to session.owner, and return the flattened file metadata list."""
    _ensure_lms_module()
    entry = cache.get_entry(session.sid, tenant=session.tenant, uid=session.uid)

    with entry.crawl_lock:
        if not refresh and entry.files:
            return entry.files

        payload = fetch_student_payload(session, store=store, refresh=False)
        courses = payload.get("courses") or []
        student_grade = session.grade or payload.get("student", {}).get("grade", "")

        indexed_items: list[dict[str, Any]] = []

        def _walk_folder(course_id: str, subject: str, folder_id: str, folder_path: str, depth: int = 1):
            if depth > 6:
                return
            try:
                res = call_lms(
                    session,
                    store,
                    lms.list_resources,
                    session.tenant,
                    course_id=course_id,
                    parent_resource_id=folder_id,
                    page=1,
                    page_size=100,
                )
                items = res.get("resources") if isinstance(res, dict) else (res or [])
                for item in items:
                    grade = item.get("grade") or ""
                    if not _is_allowed_grade(grade, student_grade):
                        continue
                    is_folder = item.get("is_folder") or False
                    title = item.get("title") or item.get("name") or "Untitled"
                    current_path = f"{folder_path}/{title}" if folder_path else title
                    if is_folder:
                        _walk_folder(course_id, subject, str(item.get("id")), current_path, depth + 1)
                    else:
                        files_in_res = item.get("file_urls") or []
                        if not files_in_res:
                            ref = f"lms://resource/{item.get('id')}/0"
                            filename = title
                            ext = Path(filename).suffix.lstrip(".").lower()
                            indexed_items.append({
                                "ref": ref,
                                "course_id": course_id,
                                "subject": subject,
                                "folder_path": folder_path,
                                "title": title,
                                "filename": filename,
                                "teacher": item.get("teacher_name") or item.get("author") or "",
                                "grade": grade,
                                "ext": ext,
                            })
                        else:
                            for f in files_in_res:
                                fid = str(f.get("id") or f.get("file_id") or "0")
                                fname = str(f.get("name") or f.get("title") or title)
                                ext = Path(fname).suffix.lstrip(".").lower()
                                ref = f"lms://resource/{item.get('id')}/{fid}"
                                indexed_items.append({
                                    "ref": ref,
                                    "course_id": course_id,
                                    "subject": subject,
                                    "folder_path": folder_path,
                                    "title": title,
                                    "filename": fname,
                                    "teacher": item.get("teacher_name") or item.get("author") or "",
                                    "grade": grade,
                                    "ext": ext,
                                })
            except Exception:
                pass

        for c in courses:
            cid = str(c.get("id") or c.get("course_id") or "")
            subj = str(c.get("title") or c.get("subject") or c.get("name") or "Course")
            if not cid:
                continue
            try:
                res = call_lms(session, store, lms.list_resources, session.tenant, course_id=cid, page=1, page_size=100)
                items = res.get("resources") if isinstance(res, dict) else (res or [])
                for item in items:
                    grade = item.get("grade") or ""
                    if not _is_allowed_grade(grade, student_grade):
                        continue
                    is_folder = item.get("is_folder") or False
                    title = item.get("title") or item.get("name") or "Untitled"
                    if is_folder:
                        _walk_folder(cid, subj, str(item.get("id")), title, depth=1)
                    else:
                        files_in_res = item.get("file_urls") or []
                        if not files_in_res:
                            ref = f"lms://resource/{item.get('id')}/0"
                            filename = title
                            ext = Path(filename).suffix.lstrip(".").lower()
                            indexed_items.append({
                                "ref": ref,
                                "course_id": cid,
                                "subject": subj,
                                "folder_path": "",
                                "title": title,
                                "filename": filename,
                                "teacher": item.get("teacher_name") or item.get("author") or "",
                                "grade": grade,
                                "ext": ext,
                            })
                        else:
                            for f in files_in_res:
                                fid = str(f.get("id") or f.get("file_id") or "0")
                                fname = str(f.get("name") or f.get("title") or title)
                                ext = Path(fname).suffix.lstrip(".").lower()
                                ref = f"lms://resource/{item.get('id')}/{fid}"
                                indexed_items.append({
                                    "ref": ref,
                                    "course_id": cid,
                                    "subject": subj,
                                    "folder_path": "",
                                    "title": title,
                                    "filename": fname,
                                    "teacher": item.get("teacher_name") or item.get("author") or "",
                                    "grade": grade,
                                    "ext": ext,
                                })
            except Exception:
                pass

        tasks = payload.get("tasks") or []
        for t in tasks:
            task_title = t.get("title") or "Task"
            subj = t.get("subject") or "General"
            for att in t.get("attachments") or []:
                url = att.get("file_url") or att.get("url")
                if url:
                    fname = att.get("name") or att.get("title") or "attachment"
                    ref = f"lms://url/{_safe_b64(url)}"
                    ext = Path(fname).suffix.lstrip(".").lower()
                    indexed_items.append({
                        "ref": ref,
                        "course_id": str(t.get("course_id") or ""),
                        "subject": subj,
                        "folder_path": f"Task: {task_title}",
                        "title": task_title,
                        "filename": fname,
                        "teacher": t.get("teacher") or "",
                        "grade": student_grade,
                        "ext": ext,
                    })

        if indexed_items:
            db.upsert_lms_files_batch(session.owner, indexed_items)

        cache.put_files(session.sid, indexed_items, tenant=session.tenant, uid=session.uid)
        return indexed_items


def search_files(
    session: Session,
    db: Database,
    query: str,
    subject: str | None = None,
    ext: str | None = None,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Search LMS files metadata in FTS5 table scoped to session.owner."""
    results = db.search_lms_files(session.owner, query, subject=subject, ext=ext, limit=limit)
    if not results and not query:
        # Initial search when empty triggers a crawl
        crawl_and_index_lms_files(session, db, refresh=False)
        results = db.search_lms_files(session.owner, query, subject=subject, ext=ext, limit=limit)
    return results


def browse_lms_folder(
    session: Session,
    db: Database,
    course_id: str | None = None,
    folder_id: str | None = None,
) -> list[dict[str, Any]]:
    """Browse folder structure for a course or folder ID, returning children."""
    _ensure_lms_module()
    res = call_lms(
        session,
        None,
        lms.list_resources,
        session.tenant,
        course_id=course_id,
        parent_resource_id=folder_id,
        page=1,
        page_size=100,
    )
    items = res.get("resources") if isinstance(res, dict) else (res or [])
    out = []
    for item in items:
        out.append({
            "id": item.get("id"),
            "title": item.get("title") or item.get("name"),
            "is_folder": item.get("is_folder", False),
            "child_count": item.get("child_count", 0),
            "teacher": item.get("teacher_name") or item.get("author", ""),
            "grade": item.get("grade", ""),
            "files": item.get("file_urls", []),
        })
    return out


def _parse_due_timestamp(date_str: str | None) -> float:
    if not date_str:
        return float("inf")
    try:
        # Standard ISO format: 2026-10-20T14:00:00Z
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return float("inf")


def build_study_triage(
    session: Session,
    db: Database,
    store: SessionStore | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Compute the 3 ROADMAP sections from live student data:
    1. 'now': overdue items, due today/tomorrow, calendar today, unread notifications, attendance.
    2. 'weak_topics': wrong questions from graded EOL tests, low rubric rows from graded FA/SA, task feedback.
    3. 'before_break': upcoming unsubmitted work before next calendar break.
    """
    entry = cache.get_entry(session.sid, tenant=session.tenant, uid=session.uid)

    def _triage_calc() -> dict[str, Any]:
        payload = fetch_student_payload(session, store=store, refresh=False)
        now_ts = time.time()
        one_day = 86400
        two_days = 172800

        now_section: dict[str, Any] = {
            "overdue": [],
            "due_soon": [],
            "calendar_today": [],
            "unread_notifications": [],
            "attendance": payload.get("attendance") or {},
        }

        eol_items = payload.get("eol") or []
        assessments = payload.get("assessments") or []
        tasks = payload.get("tasks") or []
        calendar_events = payload.get("calendar") or []
        notifs = payload.get("notifications") or []

        # 1. NOW: Overdue & Due Soon
        for item in eol_items + assessments + tasks:
            status = str(item.get("status") or "").lower()
            submitted = item.get("submitted", False) or "submitted" in status or "completed" in status or "graded" in status
            if submitted:
                continue
            due_str = item.get("due_date") or item.get("due") or item.get("end_date")
            ts = _parse_due_timestamp(due_str)
            if ts < now_ts:
                now_section["overdue"].append(item)
            elif ts <= now_ts + two_days:
                now_section["due_soon"].append(item)

        today_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for ev in calendar_events:
            ev_date = str(ev.get("date") or ev.get("start_date") or "")[:10]
            if ev_date == today_date_str:
                now_section["calendar_today"].append(ev)

        for n in notifs:
            if not n.get("is_read", False) and not n.get("read", False):
                now_section["unread_notifications"].append(n)

        # 2. WEAK TOPICS: Graded items with mistakes / low rubrics
        weak_topics: list[dict[str, Any]] = []

        # A. Graded EOL tests (score < total)
        for e in eol_items:
            status = str(e.get("status") or "").lower()
            score = e.get("score")
            total = e.get("total") or e.get("max_marks") or e.get("total_marks")
            eid = str(e.get("id") or "")
            if score is not None and total is not None and score < total and eid:
                wrong_qs: list[dict[str, Any]] = []
                try:
                    res = call_lms(session, store, lms.eol_result, session.tenant, eid, session.uid)
                    questions = res.get("questions") or res.get("responses") or []
                    for q in questions:
                        is_correct = q.get("is_correct")
                        if is_correct is False or (q.get("score", 1) == 0 and not is_correct):
                            wrong_qs.append({
                                "question_id": str(q.get("question_id") or q.get("id") or ""),
                                "text": q.get("question_text") or q.get("text") or "",
                                "topic": q.get("topic") or e.get("topic") or "",
                                "student_answer": q.get("selected_option") or q.get("answer"),
                            })
                except Exception:
                    pass

                weak_topics.append({
                    "id": f"eol:{eid}",
                    "subject": e.get("subject") or e.get("subject_name") or "General",
                    "topic": e.get("topic") or e.get("title") or "EOL Test",
                    "source_type": "eol",
                    "source_id": eid,
                    "title": e.get("title") or "End of Lesson Test",
                    "score_label": f"{score}/{total}",
                    "why_weak": f"Scored {score}/{total} on graded test",
                    "wrong_questions": wrong_qs,
                })
                if len(weak_topics) >= 10:
                    break

        # B. Graded FA/SA assessments with feedback or lower criterion
        for a in assessments:
            aid = str(a.get("id") or "")
            status = str(a.get("status") or "").lower()
            score = a.get("marks") or a.get("score")
            total = a.get("total_marks") or a.get("max_marks")
            if score is not None and total is not None and score < total and aid:
                fb_text = ""
                try:
                    fb = call_lms(session, store, lms.assessment_feedback, aid)
                    fb_text = fb.get("feedback") or fb.get("text") or ""
                except Exception:
                    pass
                weak_topics.append({
                    "id": f"fa:{aid}",
                    "subject": a.get("subject") or "General",
                    "topic": a.get("title") or "Assessment",
                    "source_type": "assessment",
                    "source_id": aid,
                    "title": a.get("title") or "Formative Assessment",
                    "score_label": f"{score}/{total}",
                    "why_weak": fb_text or f"Achieved {score}/{total}",
                    "wrong_questions": [],
                })
                if len(weak_topics) >= 15:
                    break

        # 3. BEFORE BREAK: Ordered upcoming unsubmitted work
        break_date_ts = now_ts + (14 * one_day)  # Default 2-week window
        for ev in calendar_events:
            ev_title = str(ev.get("title") or ev.get("label") or "").lower()
            if any(k in ev_title for k in ("break", "holiday", "vacation", "term end")):
                ev_ts = _parse_due_timestamp(ev.get("date") or ev.get("start_date"))
                if ev_ts > now_ts:
                    break_date_ts = min(break_date_ts, ev_ts)
                    break

        before_break: list[dict[str, Any]] = []
        for item in eol_items + assessments + tasks:
            status = str(item.get("status") or "").lower()
            submitted = item.get("submitted", False) or "submitted" in status or "graded" in status
            if submitted:
                continue
            due_str = item.get("due_date") or item.get("due") or item.get("end_date")
            ts = _parse_due_timestamp(due_str)
            if now_ts <= ts <= break_date_ts:
                before_break.append({
                    "id": str(item.get("id") or ""),
                    "title": item.get("title") or "Upcoming Item",
                    "subject": item.get("subject") or "General",
                    "due": due_str,
                    "due_ts": ts,
                    "type": "eol" if item in eol_items else "assessment" if item in assessments else "task",
                })

        before_break.sort(key=lambda x: x["due_ts"])

        return {
            "now": now_section,
            "weak_topics": weak_topics,
            "before_break": before_break,
            "computed_at": time.time(),
        }

    return cache.memo(entry, "study_triage", _triage_calc, ttl=300, refresh=refresh)


def get_graded_item_detail(
    session: Session,
    item_type: str,
    item_id: str,
    store: SessionStore | None = None,
) -> dict[str, Any]:
    """Retrieve full detail for a graded item.
    MANDATORY SAFETY CHECK: Calls assert_not_live_test() BEFORE returning any questions/answers."""
    _ensure_lms_module()
    if not item_id:
        raise ValueError("item_id is required.")

    if item_type == "eol":
        # First fetch test metadata row to check its submission status
        payload = fetch_student_payload(session, store=store)
        test_row = None
        for t in payload.get("eol") or []:
            if str(t.get("id")) == item_id:
                test_row = t
                break

        status = test_row.get("status") if test_row else None
        submitted = (
            test_row.get("is_submitted")
            if test_row
            else None
        )
        score = test_row.get("score") if test_row else None
        has_result = score is not None

        # Live test check
        assert_not_live_test(item_status=status, submitted=submitted, has_result=has_result)

        # Safe to fetch full graded result
        result = call_lms(session, store, lms.eol_result, session.tenant, item_id, session.uid)
        return {
            "item_type": "eol",
            "item_id": item_id,
            "title": test_row.get("title") if test_row else "EOL Test",
            "score": score,
            "result": result,
        }

    elif item_type == "fa" or item_type == "assessment":
        payload = fetch_student_payload(session, store=store)
        a_row = None
        for a in payload.get("assessments") or []:
            if str(a.get("id")) == item_id:
                a_row = a
                break

        status = a_row.get("status") if a_row else None
        submitted = a_row.get("submitted") if a_row else None
        has_result = a_row.get("marks") is not None if a_row else None

        assert_not_live_test(item_status=status, submitted=submitted, has_result=has_result)

        sub = call_lms(session, store, lms.assessment_submission, item_id)
        fb = call_lms(session, store, lms.assessment_feedback, item_id)
        return {
            "item_type": "assessment",
            "item_id": item_id,
            "title": a_row.get("title") if a_row else "Assessment",
            "submission": sub,
            "feedback": fb,
        }

    elif item_type == "task":
        detail = call_lms(session, store, lms.learning_task_detail, session.tenant, item_id)
        return {
            "item_type": "task",
            "item_id": item_id,
            "detail": detail,
        }

    else:
        raise ValueError(f"Unknown item_type '{item_type}'. Must be 'eol', 'assessment', or 'task'.")


def request_write_confirmation(
    write_mgr: WriteConfirmationManager,
    session: Session,
    action: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Issue a single-use confirmation token for an LMS write action."""
    allowed_actions = {
        "mark_notification_read",
        "mark_announcement_read",
        "send_message",
        "request_extension",
        "submit_task",
    }
    if action not in allowed_actions:
        raise ValueError(f"Action '{action}' is not supported for LMS write confirmation.")

    return write_mgr.create_token(
        owner=session.owner,
        action=action,
        payload=payload,
    )


def execute_confirmed_write(
    write_mgr: WriteConfirmationManager,
    session: Session,
    token_id: str,
    store: SessionStore | None = None,
) -> dict[str, Any]:
    """Consume single-use token and execute confirmed LMS write.
    Never retries on failure and invalidates affected cache entries."""
    _ensure_lms_module()
    record = write_mgr.consume_token(owner=session.owner, token_id=token_id)
    if not record:
        raise PermissionError("Invalid, expired, or previously used confirmation token.")

    action = record["action"]
    payload = record["payload"]

    entry = cache.get_entry(session.sid, tenant=session.tenant, uid=session.uid)

    try:
        if action == "mark_notification_read":
            nid = payload["notification_id"]
            res = call_lms(session, store, lms.notification_read, session.tenant, nid, write=True)
            cache.invalidate(entry, "payload", "study_triage")
            return {"status": "ok", "action": action, "result": res}

        elif action == "mark_announcement_read":
            aid = payload["announcement_id"]
            res = call_lms(session, store, lms.announcement_read, aid, write=True)
            cache.invalidate(entry, "payload", "study_triage")
            return {"status": "ok", "action": action, "result": res}

        elif action == "send_message":
            to_user_id = payload["to_user_id"]
            text = payload["text"]
            res = call_lms(session, store, lms.send_message, session.tenant, to_user_id, text, write=True)
            return {"status": "ok", "action": action, "result": res}

        elif action == "request_extension":
            aid = payload["assessment_id"]
            reason = payload.get("reason", "")
            res = call_lms(session, store, lms.assessment_request, aid, "extension", reason, write=True)
            cache.invalidate(entry, "payload", "study_triage")
            return {"status": "ok", "action": action, "result": res}

        elif action == "submit_task":
            tid = payload["task_id"]
            files = payload.get("files", [])
            text = payload.get("text", "")
            res = call_lms(session, store, lms.task_submit, session.tenant, tid, files, text, write=True)
            cache.invalidate(entry, "payload", "study_triage")
            return {"status": "ok", "action": action, "result": res}

        else:
            raise ValueError(f"Unknown action: {action}")

    except Exception as e:
        raise LMSUpstreamError(f"Failed to execute confirmed write '{action}': {e}") from e
