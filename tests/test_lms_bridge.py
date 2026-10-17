# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Indus LMS bridge and study triage logic."""

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.cache import clear as clear_cache, set_store
from app.db import Database
from app.lms_bridge import (
    LMSNotAuthenticatedError,
    LMSUpstreamError,
    _is_allowed_grade,
    browse_lms_folder,
    build_study_triage,
    call_lms,
    crawl_and_index_lms_files,
    execute_confirmed_write,
    fetch_student_payload,
    get_graded_item_detail,
    login_indus,
    request_write_confirmation,
    search_files,
)
from app.security import WriteConfirmationManager
from app.sessions import Session, SessionStore


@pytest.fixture(autouse=True)
def reset_in_memory_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def test_db(tmp_path):
    return Database(tmp_path / "doomed.sqlite")


@pytest.fixture
def session_store(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite")
    set_store(store)
    return store


@pytest.fixture
def mock_session(session_store):
    sess = Session(
        access="mock_access_token_123",
        refresh="mock_refresh_token_456",
        tenant="tenant_indus_1",
        name="Student One",
        email="student1@school.edu",
        uid="u_001",
        grade="Grade 11",
    )
    session_store.add(sess)
    return sess


def test_grade_filter():
    assert _is_allowed_grade(None, "Grade 11") is True
    assert _is_allowed_grade("Grade 11", "Grade 11") is True
    assert _is_allowed_grade("Grade 11 Physics", "Grade 11") is True
    assert _is_allowed_grade("Grade 12 Physics", "Grade 11") is False
    assert _is_allowed_grade("Grade 10 Chem", "Grade 11") is False


def test_unauthenticated_error():
    empty_session = Session("", "", "")
    with pytest.raises(LMSNotAuthenticatedError):
        call_lms(empty_session, None, lambda tok: None)

    with pytest.raises(LMSNotAuthenticatedError):
        fetch_student_payload(empty_session)


def test_call_lms_401_refresh_retry(mock_session, session_store, monkeypatch):
    import app.lms_bridge as bridge

    fake_lms = MagicMock()
    mock_fn = MagicMock()

    class FakeLMSError(Exception):
        def __init__(self, status):
            self.status = status

    # First call returns 401, refresh succeeds, second call succeeds
    mock_fn.side_effect = [FakeLMSError(401), {"ok": True}]
    fake_lms.refresh_token.return_value = {"access": "new_access_token", "refresh": "new_ref_tok"}
    monkeypatch.setattr(bridge, "lms", fake_lms)

    res = call_lms(mock_session, session_store, mock_fn, write=False)
    assert res == {"ok": True}
    assert mock_fn.call_count == 2
    assert mock_session.access == "new_access_token"

    # Writes never retry on 401
    mock_write_fn = MagicMock(side_effect=FakeLMSError(401))
    with pytest.raises(LMSUpstreamError):
        call_lms(mock_session, session_store, mock_write_fn, write=True)
    assert mock_write_fn.call_count == 1


def test_crawl_and_index_lms_files(mock_session, test_db, session_store, monkeypatch):
    import app.lms_bridge as bridge

    fake_lms = MagicMock()
    fake_lms.my_courses.return_value = {
        "courses": [{"id": "c1", "title": "Physics HL", "course_id": "c1"}]
    }
    fake_lms.eol_tests.return_value = {"results": []}
    fake_lms.assessments.return_value = {"results": []}
    fake_lms.learning_tasks.return_value = []
    fake_lms.calendar_events.return_value = {"events": []}
    fake_lms.attendance_day.return_value = {}
    fake_lms.notifications.return_value = {"notifications": []}
    fake_lms.announcements.return_value = {"announcements": []}

    fake_lms.list_resources.return_value = {
        "resources": [
            {
                "id": "res_1",
                "title": "Kinematics Notes",
                "grade": "Grade 11",
                "is_folder": False,
                "file_urls": [{"id": "f_1", "name": "kinematics.pdf"}],
            },
            {
                "id": "res_other_grade",
                "title": "Grade 12 Only",
                "grade": "Grade 12",
                "is_folder": False,
                "file_urls": [{"id": "f_bad", "name": "bad.pdf"}],
            },
        ]
    }
    monkeypatch.setattr(bridge, "lms", fake_lms)

    files = crawl_and_index_lms_files(mock_session, test_db, session_store, refresh=True)
    assert len(files) == 1
    assert files[0]["ref"] == "lms://resource/res_1/f_1"

    # Search in DB
    found = search_files(mock_session, test_db, "kinematics")
    assert len(found) >= 1
    assert found[0]["ref"] == "lms://resource/res_1/f_1"

    # Other owner cannot see it
    other_session = Session("tok", "ref", "tenant_other", name="Student Two", email="s2@school.edu", uid="u_002", grade="Grade 11")
    other_found = search_files(other_session, test_db, "kinematics")
    assert len(other_found) == 0


def test_live_test_lockout(mock_session, monkeypatch):
    import app.lms_bridge as bridge

    fake_lms = MagicMock()
    # Unsubmitted / in-progress EOL test
    fake_lms.eol_tests.return_value = {
        "results": [
            {"id": "test_live_1", "title": "Live EOL", "status": "in_progress", "is_submitted": False}
        ]
    }
    fake_lms.my_courses.return_value = {"courses": []}
    fake_lms.assessments.return_value = {"results": []}
    fake_lms.learning_tasks.return_value = []
    fake_lms.calendar_events.return_value = {"events": []}
    fake_lms.attendance_day.return_value = {}
    fake_lms.notifications.return_value = {"notifications": []}
    fake_lms.announcements.return_value = {"announcements": []}
    monkeypatch.setattr(bridge, "lms", fake_lms)

    # Calling get_graded_item_detail on live test MUST raise PermissionError
    with pytest.raises(PermissionError) as exc_info:
        get_graded_item_detail(mock_session, "eol", "test_live_1")
    assert "Live test protection" in str(exc_info.value)
    assert fake_lms.eol_result.call_count == 0


def test_build_study_triage(mock_session, test_db, session_store, monkeypatch):
    import app.lms_bridge as bridge

    fake_lms = MagicMock()
    now_ts = time.time()
    fake_lms.my_courses.return_value = {"courses": []}
    fake_lms.eol_tests.return_value = {
        "results": [
            {
                "id": "eol_done_1",
                "title": "Vectors Quiz",
                "status": "completed",
                "score": 3,
                "total": 5,
                "subject": "Physics",
            }
        ]
    }
    fake_lms.eol_result.return_value = {
        "questions": [
            {"question_id": "q1", "question_text": "Vector addition", "is_correct": False, "score": 0}
        ]
    }
    fake_lms.assessments.return_value = {
        "results": [
            {
                "id": "fa_due_soon",
                "title": "Lab Report",
                "status": "pending",
                "due_date": "2026-10-18T18:00:00Z",
                "submitted": False,
            }
        ]
    }
    fake_lms.learning_tasks.return_value = []
    fake_lms.calendar_events.return_value = {
        "events": [
            {"title": "Diwali Break", "date": "2026-10-25T00:00:00Z"}
        ]
    }
    fake_lms.attendance_day.return_value = {"present": True}
    fake_lms.notifications.return_value = {
        "notifications": [{"id": "n1", "title": "Homework posted", "is_read": False}]
    }
    fake_lms.announcements.return_value = {"announcements": []}
    monkeypatch.setattr(bridge, "lms", fake_lms)

    triage = build_study_triage(mock_session, test_db, session_store, refresh=True)

    assert "now" in triage
    assert "weak_topics" in triage
    assert "before_break" in triage

    assert len(triage["now"]["unread_notifications"]) == 1
    assert len(triage["weak_topics"]) == 1
    assert triage["weak_topics"][0]["score_label"] == "3/5"
    assert len(triage["weak_topics"][0]["wrong_questions"]) == 1

    assert len(triage["before_break"]) == 1
    assert triage["before_break"][0]["id"] == "fa_due_soon"


def test_write_confirmation_flow(mock_session, monkeypatch):
    import app.lms_bridge as bridge

    fake_lms = MagicMock()
    fake_lms.notification_read.return_value = {"success": True}
    monkeypatch.setattr(bridge, "lms", fake_lms)

    write_mgr = WriteConfirmationManager()
    token_dict = request_write_confirmation(
        write_mgr,
        mock_session,
        "mark_notification_read",
        {"notification_id": "notif_99"},
    )
    token_id = token_dict["token_id"]

    res = execute_confirmed_write(write_mgr, mock_session, token_id)
    assert res["status"] == "ok"
    assert fake_lms.notification_read.call_count == 1

    # Replay must fail
    with pytest.raises(PermissionError):
        execute_confirmed_write(write_mgr, mock_session, token_id)
