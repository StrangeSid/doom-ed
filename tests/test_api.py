# SPDX-License-Identifier: GPL-3.0-or-later
"""FastAPI endpoint integration tests."""

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.cache import clear as clear_cache
from app.db import Database
from app.main import app, db, session_store
from app.sessions import Session


@pytest.fixture(autouse=True)
def clean_state():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_cookie(tmp_path):
    sess = Session(
        access="live_access_123",
        refresh="live_refresh_456",
        tenant="tenant_indus_1",
        name="Sid Student",
        email="sid@school.edu",
        uid="u_sid_01",
        grade="Grade 11",
    )
    session_store.add(sess)
    return sess.sid


def test_session_unauthenticated(client, monkeypatch):
    monkeypatch.setenv("DOOMED_AUTO_BOOTSTRAP_TOKEN", "0")
    res = client.get("/api/session")
    assert res.status_code == 401


def test_session_authenticated(client, auth_cookie):
    client.cookies.set("doomed_sid", auth_cookie)
    res = client.get("/api/session")
    assert res.status_code == 200
    data = res.json()
    assert data["student"]["name"] == "Sid Student"
    assert data["student"]["email"] == "sid@school.edu"


def test_study_triage_endpoint(client, auth_cookie, monkeypatch):
    import app.lms_bridge as bridge

    fake_lms = MagicMock()
    fake_lms.my_courses.return_value = {"courses": []}
    fake_lms.eol_tests.return_value = {"results": []}
    fake_lms.assessments.return_value = {"results": []}
    fake_lms.learning_tasks.return_value = []
    fake_lms.calendar_events.return_value = {"events": []}
    fake_lms.notifications.return_value = {"notifications": []}
    fake_lms.announcements.return_value = {"announcements": []}
    monkeypatch.setattr(bridge, "lms", fake_lms)

    client.cookies.set("doomed_sid", auth_cookie)
    res = client.get("/api/study")
    assert res.status_code == 200
    json_data = res.json()
    assert json_data["status"] == "ok"
    assert "now" in json_data["data"]
    assert "weak_topics" in json_data["data"]
    assert "before_break" in json_data["data"]


def test_threads_crud(client, auth_cookie):
    client.cookies.set("doomed_sid", auth_cookie)

    # 1. Create thread
    res = client.post("/api/threads", json={"title": "Kinematics Lesson", "topic": "Physics"})
    assert res.status_code == 200
    t = res.json()["thread"]
    thread_id = t["id"]
    assert t["title"] == "Kinematics Lesson"

    # 2. List threads
    list_res = client.get("/api/threads")
    assert list_res.status_code == 200
    assert any(x["id"] == thread_id for x in list_res.json()["threads"])

    # 3. Get thread detail
    get_res = client.get(f"/api/threads/{thread_id}")
    assert get_res.status_code == 200
    assert get_res.json()["thread"]["id"] == thread_id

    # 4. Delete thread
    del_res = client.delete(f"/api/threads/{thread_id}")
    assert del_res.status_code == 200
    assert client.get(f"/api/threads/{thread_id}").status_code == 404


def test_file_upload_and_acl(client, auth_cookie):
    client.cookies.set("doomed_sid", auth_cookie)

    file_content = b"Sample text for uploaded notes."
    files = {"file": ("notes.txt", io.BytesIO(file_content), "text/plain")}

    upload_res = client.post("/api/upload", files=files)
    assert upload_res.status_code == 200
    up_data = upload_res.json()
    sha256 = up_data["sha256"]

    # Authenticated student can download
    get_res = client.get(f"/api/files/{sha256}")
    assert get_res.status_code == 200
    assert get_res.content == file_content

    # Another student session on different tenant cannot download (403)
    other_sess = Session("tok2", "ref2", "tenant_other", name="Other", email="other@school.edu", uid="u_other")
    session_store.add(other_sess)
    client.cookies.set("doomed_sid", other_sess.sid)

    blocked_res = client.get(f"/api/files/{sha256}")
    assert blocked_res.status_code == 403


def test_ib_proxy_endpoints(client, monkeypatch):
    import app.ib_client as ibc

    fake_search = {
        "status": "ok",
        "data": [{"sha256": "abc12345" * 8, "title": "Physics Paper 1", "rtype": "paper"}],
        "meta": {"total": 1},
    }
    monkeypatch.setattr(ibc, "search_ib", lambda **kwargs: fake_search)

    res = client.get("/api/ib/search?q=physics")
    assert res.status_code == 200
    assert res.json()["data"]["data"][0]["title"] == "Physics Paper 1"


def test_start_local_session(client):
    """Local session creates valid independent student profile without LMS."""
    res = client.post(
        "/api/session/local",
        json={
            "name": "Alex Candidate",
            "grade": "DP 2",
            "subjects": ["Physics HL", "Mathematics: Analysis and Approaches HL"],
            "target_session": "May 2026",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["student"]["name"] == "Alex Candidate"
    assert data["student"]["is_lms_connected"] is False
    assert "Physics HL" in data["student"]["subjects"]

    # Cookie was set and session endpoint returns student
    sess_res = client.get("/api/session")
    assert sess_res.status_code == 200
    assert sess_res.json()["student"]["name"] == "Alex Candidate"
    assert sess_res.json()["student"]["is_lms_connected"] is False


def test_standalone_study_triage(client):
    """Study triage works independently for local sessions with zero LMS credentials."""
    client.post(
        "/api/session/local",
        json={"name": "Independent Learner", "grade": "DP 2"},
    )
    res = client.get("/api/study")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["lms_connected"] is False
    assert len(data["now"]["due_soon"]) > 0
    assert len(data["weak_topics"]) > 0
    assert len(data["before_break"]) > 0
    assert "May 2026" in data["now"]["exam_countdown"]


def test_update_profile(client):
    """Students can update their chosen IB subjects and exam session."""
    client.post("/api/session/local")
    res = client.post(
        "/api/profile",
        json={
            "name": "Updated Name",
            "grade": "DP 1",
            "subjects": ["Economics HL", "History HL"],
            "target_session": "Nov 2026",
        },
    )
    assert res.status_code == 200
    student = res.json()["student"]
    assert student["name"] == "Updated Name"
    assert student["grade"] == "DP 1"
    assert student["subjects"] == ["Economics HL", "History HL"]
    assert student["target_session"] == "Nov 2026"
