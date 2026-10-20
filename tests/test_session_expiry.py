# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the expired-session behaviour.

The Indus LMS token expires on its own schedule. When it does, doom-ed must
keep the student's last known data on screen, say plainly that it is not live,
and ask for a fresh sign-in.
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app import cache
from app.lms_bridge import LMSNotAuthenticatedError, build_study_triage
from app.sessions import Session, SessionStore


@pytest.fixture
def test_store(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite")
    cache.set_store(store)
    yield store
    cache.clear()


@pytest.fixture
def session(test_store):
    sess = Session(
        access="expired_access",
        refresh="expired_refresh",
        tenant="tenant_x",
        name="Test Student",
        email="t@school.edu",
        uid="u_t",
        grade="Grade 11",
    )
    test_store.add(sess)
    return sess


def _seed_payload(store, session, payload: dict) -> None:
    """Write a payload into the L2 cache as if a previous fetch had succeeded.

    Aged past the fresh window (300s) but inside the persistent window
    (3600s), which is the range where stale data is still allowed.
    """
    key = f"payload:{session.tenant}:{session.uid}"
    store.set_cache_at(key, json.dumps(payload), time.time() - 1800)


def test_stale_payload_is_served_and_flagged(test_store, session, monkeypatch):
    """An expired token must not empty the screen. The stale data comes back
    with a flag saying it is not live."""
    import app.lms_bridge as bridge

    _seed_payload(
        test_store,
        session,
        {
            "student": {"name": "Test Student", "email": "t@school.edu"},
            "eol": [],
            "assessments": [],
            "tasks": [],
            "calendar": [],
            "notifications": [],
            "announcements": [],
        },
    )

    def _expired(*args, **kwargs):
        raise LMSNotAuthenticatedError("Indus LMS refresh token is expired or invalid.")

    monkeypatch.setattr(bridge, "call_lms", _expired)

    payload = bridge.fetch_student_payload(session, test_store, refresh=True)
    assert payload["student"]["name"] == "Test Student"
    assert "_auth_error" in payload
    assert "expired" in payload["_auth_error"]


def test_expired_token_without_a_cache_raises(test_store, session, monkeypatch):
    """With nothing cached, the caller must get a real auth error rather than
    an empty payload dressed up as data."""
    import app.lms_bridge as bridge

    def _expired(*args, **kwargs):
        raise LMSNotAuthenticatedError("Indus LMS refresh token is expired or invalid.")

    monkeypatch.setattr(bridge, "call_lms", _expired)

    with pytest.raises(LMSNotAuthenticatedError):
        bridge.fetch_student_payload(session, test_store, refresh=True)


def test_triage_reports_the_auth_failure(test_store, session, monkeypatch, tmp_path):
    """The triage carries the auth flag so the API can tell the browser."""
    import app.lms_bridge as bridge
    from app.db import Database

    _seed_payload(
        test_store,
        session,
        {
            "student": {"name": "Test Student"},
            "eol": [],
            "assessments": [],
            "tasks": [],
            "calendar": [],
            "notifications": [],
            "announcements": [],
        },
    )

    def _expired(*args, **kwargs):
        raise LMSNotAuthenticatedError("Indus LMS refresh token is expired or invalid.")

    monkeypatch.setattr(bridge, "call_lms", _expired)

    db = Database(tmp_path / "doomed.sqlite")
    triage = build_study_triage(session, db, test_store, refresh=True)
    assert "_auth_error" in triage
    assert triage["now"]["overdue"] == []
    db.close()


def test_stale_data_is_not_served_forever(test_store, session, monkeypatch):
    """Past the persistent-cache window the app must refuse the stale copy
    and ask for a sign-in instead."""
    import app.lms_bridge as bridge

    key = f"payload:{session.tenant}:{session.uid}"
    # Far older than PAYLOAD_PERSIST_TTL.
    test_store.set_cache_at(
        key,
        json.dumps({"student": {"name": "Old"}, "eol": [], "assessments": [], "tasks": []}),
        time.time() - 100000,
    )

    def _expired(*args, **kwargs):
        raise LMSNotAuthenticatedError("Indus LMS refresh token is expired or invalid.")

    monkeypatch.setattr(bridge, "call_lms", _expired)

    with pytest.raises(LMSNotAuthenticatedError):
        bridge.fetch_student_payload(session, test_store, refresh=True)
