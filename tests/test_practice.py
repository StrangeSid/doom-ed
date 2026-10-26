# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the adaptive practice engine, feed API, and optional LMS plugin."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.practice.grading import (
    bkt_update,
    elo_update,
    grade_answer,
    mastery_band,
    scheduler_update,
    update_state,
)
from app.practice.models import Item, Skill, StudentState
from app.practice.planner import select_cards
from app.practice.service import ConflictError, PracticeService
from app.practice.store import PracticeStore
from app.sessions import Session

OWNER = "tenant_practice:u_practice"


def _skill(skill_id: str = "phys.kin", *, subject: str = "Physics HL", prereqs: tuple[str, ...] = ()) -> Skill:
    return Skill(
        id=skill_id,
        subject=subject,
        title=skill_id.replace(".", " ").title(),
        prereq_ids=prereqs,
        exam_weight=0.8,
    )


def _item(
    item_id: str,
    *,
    owner: str = OWNER,
    skill_ids: tuple[str, ...] = ("phys.kin",),
    correct: list[int] | None = None,
    difficulty: float = 0.0,
    verification: str = "verified",
    content_hash: str = "",
) -> Item:
    return Item(
        id=item_id,
        skill_ids=skill_ids,
        format="mcq_single",
        stem=f"Question {item_id}",
        options=(
            {"index": 1, "label": "Option one"},
            {"index": 2, "label": "Option two"},
        ),
        solution={"correct": correct or [1], "explanation": "Because one is correct."},
        owner=owner,
        difficulty=difficulty,
        verification=verification,
        content_hash=content_hash or f"hash-{item_id}",
    )


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "practice.sqlite")
    store = PracticeStore(db)
    service = PracticeService(db, store=store)
    yield db, store, service
    db.close()


def _seed_basic(store: PracticeStore, owner: str = OWNER) -> None:
    store.upsert_skill(_skill())
    store.insert_item(_item("item-1", owner=owner))
    store.insert_item(_item("item-2", owner=owner, difficulty=0.3))


# ---------------------------------------------------------------------------
# Pure grading and scheduling
# ---------------------------------------------------------------------------


def test_grade_mcq_single_and_dont_know():
    item = _item("g1")
    assert grade_answer(item, [1]).correct is True
    assert grade_answer(item, [2]).correct is False
    unknown = grade_answer(item, ["__dont_know__"])
    assert unknown.dont_know is True and unknown.correct is False
    with pytest.raises(ValueError):
        grade_answer(item, [9])


def test_grade_numeric_tolerance_and_unknown():
    item = Item(
        id="num-1",
        skill_ids=("phys.kin",),
        format="numeric",
        stem="What is 2+2?",
        options=(),
        solution={"value": 4, "tolerance": 0.01, "explanation": "Arithmetic."},
        owner=OWNER,
        verification="verified",
        content_hash="num-hash",
    )
    assert grade_answer(item, 4).correct is True
    assert grade_answer(item, "4.005").correct is True
    assert grade_answer(item, 4.5).correct is False
    assert grade_answer(item, "__dont_know__").dont_know is True
    with pytest.raises(ValueError):
        grade_answer(item, "not a number")


def test_bkt_elo_and_scheduler_move_in_the_right_direction():
    assert bkt_update(0.35, True, False) > 0.35
    assert bkt_update(0.75, False, False) < 0.75
    assert 0.01 <= bkt_update(0.5, False, True) <= 0.99
    theta, difficulty = elo_update(0.0, 0.0, True, attempts=0, item_attempts=0)
    assert theta > 0.0 and difficulty < 0.0
    theta2, difficulty2 = elo_update(0.0, 0.0, False, attempts=0, item_attempts=0)
    assert theta2 < 0.0 and difficulty2 > 0.0
    now = 1_800_000_000.0
    stability, due = scheduler_update(1.0, 0.5, True, False, now=now)
    assert stability > 1.0 and due > now
    failed_stability, failed_due = scheduler_update(2.0, 0.5, False, False, now=now)
    assert failed_stability < 2.0 and failed_due == now + 600
    assert mastery_band(0.8) == "solid"
    assert mastery_band(0.5) == "learning"
    assert mastery_band(0.2) == "weak"


def test_update_state_records_attempt_and_due_time():
    skill = _skill()
    item = _item("state-1")
    state = StudentState(owner=OWNER, skill_id=skill.id)
    updated, new_difficulty = update_state(
        state,
        item=item,
        correct=True,
        dont_know=False,
        item_attempts=0,
        now=1_800_000_000.0,
    )
    assert updated.attempts == 1
    assert updated.correct == 1
    assert updated.mastery > state.mastery
    assert updated.due_at and updated.due_at > 1_800_000_000.0
    assert new_difficulty <= item.difficulty


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def test_planner_is_deterministic_for_a_seed():
    skills = {"phys.kin": _skill()}
    items = [_item(f"p{i}", difficulty=i / 10) for i in range(6)]
    kwargs = dict(
        owner=OWNER,
        states={},
        skills=skills,
        items=items,
        enrolled_subjects={"Physics HL"},
        now=1_800_000_000.0,
        seed=1234,
        goal=4,
    )
    first = select_cards(**kwargs)
    second = select_cards(**kwargs)
    assert [card.item_id for card in first] == [card.item_id for card in second]


def test_planner_blocks_unmet_prerequisites():
    skills = {
        "phys.kin": _skill(),
        "phys.vec": _skill("phys.vec", prereqs=("phys.kin",)),
    }
    items = [_item("root", skill_ids=("phys.kin",)), _item("child", skill_ids=("phys.vec",))]
    blocked = select_cards(
        owner=OWNER,
        states={"phys.kin": StudentState(OWNER, "phys.kin", mastery=0.2)},
        skills=skills,
        items=items,
        enrolled_subjects={"Physics HL"},
        now=1_800_000_000.0,
        seed=7,
        goal=5,
    )
    assert [card.item_id for card in blocked] == ["root"]

    open_states = {"phys.kin": StudentState(OWNER, "phys.kin", mastery=0.8)}
    allowed = select_cards(
        owner=OWNER,
        states=open_states,
        skills=skills,
        items=items,
        enrolled_subjects={"Physics HL"},
        now=1_800_000_000.0,
        seed=7,
        goal=5,
    )
    assert "child" in [card.item_id for card in allowed]


def test_planner_respects_daily_cap_and_subject_scope():
    skills = {"phys.kin": _skill(), "chem.bond": Skill(id="chem.bond", subject="Chemistry SL", title="Bonding")}
    items = [_item("p1"), _item("p2"), _item("c1", skill_ids=("chem.bond",))]
    cards = select_cards(
        owner=OWNER,
        states={},
        skills=skills,
        items=items,
        enrolled_subjects={"Physics HL"},
        attempts_by_skill_today={"phys.kin": 3},
        now=1_800_000_000.0,
        seed=1,
        goal=5,
    )
    assert cards == []


def test_planner_ignores_unverified_and_foreign_owner_items():
    skills = {"phys.kin": _skill()}
    items = [
        _item("draft", verification="draft"),
        _item("other", owner="tenant_other:u_other"),
        _item("mine"),
    ]
    cards = select_cards(
        owner=OWNER,
        states={},
        skills=skills,
        items=items,
        enrolled_subjects={"Physics HL"},
        now=1_800_000_000.0,
        seed=3,
        goal=5,
    )
    assert [card.item_id for card in cards] == ["mine"]


# ---------------------------------------------------------------------------
# Store and service
# ---------------------------------------------------------------------------


def test_feed_is_persistent_and_never_leaks_the_answer(env):
    _, store, service = env
    _seed_basic(store)
    first = service.plan_feed(OWNER, subjects=["Physics HL"], day="2026-10-25", limit=10)
    assert first["plan"]["total_cards"] == 2
    assert first["plan"]["needs_calibration"] is False
    serialized = json.dumps(first)
    assert "explanation" not in serialized
    assert "correct_options" not in serialized
    card = first["cards"][0]
    assert card["item"]["id"] in {"item-1", "item-2"}
    second = service.plan_feed(OWNER, subjects=["Physics HL"], day="2026-10-25", limit=10)
    assert [c["card_id"] for c in second["cards"]] == [c["card_id"] for c in first["cards"]]


def test_attempt_updates_mastery_and_is_idempotent(env):
    _, store, service = env
    _seed_basic(store)
    feed = service.plan_feed(OWNER, subjects=["Physics HL"], day="2026-10-25", limit=10)
    card = feed["cards"][0]
    result = service.grade_attempt(
        OWNER,
        card_id=card["card_id"],
        item_id=card["item"]["id"],
        item_version=card["item"]["version"],
        selected=[1],
        attempt_token="token-correct",
    )
    assert result["correct"] is True
    assert result["explanation"]
    state = store.get_state(OWNER, "phys.kin")
    assert state is not None and state.attempts == 1

    replay = service.grade_attempt(
        OWNER,
        card_id=card["card_id"],
        item_id=card["item"]["id"],
        item_version=card["item"]["version"],
        selected=[1],
        attempt_token="token-correct",
    )
    assert replay["attempt_id"] == result["attempt_id"]
    assert store.get_state(OWNER, "phys.kin").attempts == 1

    with pytest.raises(ConflictError):
        service.grade_attempt(
            OWNER,
            card_id=card["card_id"],
            item_id=card["item"]["id"],
            item_version=card["item"]["version"],
            selected=[2],
            attempt_token="token-second",
        )

    # A token is bound to the card/item it graded. Reusing it elsewhere is not
    # a free replay of the old result.
    other = feed["cards"][1] if len(feed["cards"]) > 1 else card
    with pytest.raises(ConflictError):
        service.grade_attempt(
            OWNER,
            card_id=other["card_id"],
            item_id=other["item"]["id"],
            item_version=other["item"]["version"],
            selected=[1],
            attempt_token="token-correct",
        )


def test_wrong_answer_queues_a_repair(env):
    _, store, service = env
    _seed_basic(store)
    feed = service.plan_feed(OWNER, subjects=["Physics HL"], day="2026-10-25", limit=10)
    card = feed["cards"][0]
    result = service.grade_attempt(
        OWNER,
        card_id=card["card_id"],
        item_id=card["item"]["id"],
        item_version=card["item"]["version"],
        selected=[2],
        attempt_token="token-wrong",
    )
    assert result["correct"] is False
    assert result["follow_up"]["kind"] == "repair"


def test_owner_isolation_for_items_state_and_feed(env):
    _, store, service = env
    _seed_basic(store, owner=OWNER)
    other = "tenant_other:u_other"
    assert store.get_item("item-1", other) is None
    assert service.plan_feed(other, subjects=["Physics HL"], day="2026-10-25", limit=10)["plan"]["total_cards"] == 0
    other_feed = service.plan_feed(other, subjects=["Physics HL"], day="2026-10-25", limit=10)
    assert other_feed["cards"] == []
    assert service.progress(other)["subjects"] == []


def test_teach_creates_a_thread_with_practice_origin(env):
    db, store, service = env
    _seed_basic(store)
    result = service.teach(OWNER, skill_ids=["phys.kin"], intent="fix")
    assert result["thread_id"]
    thread = db.get_thread(OWNER, result["thread_id"])
    assert thread is not None
    assert thread["origin"] == "practice"
    assert "phys.kin" in (thread["origin_json"] or "")


def test_empty_pool_reports_calibration_and_fills_later(env):
    _, store, service = env
    empty = service.plan_feed(OWNER, subjects=["Physics HL"], day="2026-10-25", limit=10)
    assert empty["plan"]["needs_calibration"] is True
    _seed_basic(store)
    filled = service.plan_feed(OWNER, subjects=["Physics HL"], day="2026-10-25", limit=10)
    assert filled["plan"]["needs_calibration"] is False
    assert filled["plan"]["total_cards"] == 2


def test_settings_events_and_reset(env):
    _, store, service = env
    _seed_basic(store)
    service.plan_feed(OWNER, subjects=["Physics HL"], day="2026-10-25", limit=10)
    settings = service.update_settings(OWNER, daily_cards=99, timezone="Not/AZone", target_retention=0.99)
    assert settings["daily_cards"] == 30
    assert settings["timezone"] == "UTC"
    assert settings["target_retention"] == 0.95
    accepted = service.add_events(
        OWNER,
        [{"type": "dwell", "value": {"ms": 1000}}, {"type": "not-allowed"}],
    )
    assert accepted == 1
    reset = service.reset(OWNER, confirm=True)
    assert reset["status"] == "ok"
    assert service.plan_feed(OWNER, subjects=["Physics HL"], day="2026-10-26", limit=10)["plan"]["needs_calibration"] is True


# ---------------------------------------------------------------------------
# API and plugin boundary
# ---------------------------------------------------------------------------


def test_practice_api_requires_flag_and_supports_flow(monkeypatch):
    import app.main as main

    monkeypatch.setenv("DOOMED_PRACTICE_ENGINE", "0")
    client = TestClient(main.app)
    assert client.get("/api/home/feed").status_code == 404

    monkeypatch.setenv("DOOMED_PRACTICE_ENGINE", "1")
    session = Session(
        access="",
        refresh="",
        tenant="tenant_api_practice",
        name="API Practice",
        email="api-practice@example.com",
        uid="u_api_practice",
        grade="DP 2",
        subjects=["Physics HL"],
        target_session="May 2027",
    )
    main.session_store.add(session)
    client.cookies.set("doomed_sid", session.sid)
    store = main.practice_service.store
    store.upsert_skill(_skill("api.kin"))
    store.insert_item(_item("api-item", owner=session.owner, skill_ids=("api.kin",)))

    integrations = client.get("/api/integrations")
    assert integrations.status_code == 200
    assert any(plugin["id"] == "indus" for plugin in integrations.json()["plugins"])

    feed = client.get("/api/home/feed?day=2026-10-25&limit=10")
    assert feed.status_code == 200
    body = feed.json()
    assert body["plan"]["total_cards"] == 1
    card = body["cards"][0]
    assert "explanation" not in json.dumps(card)

    attempt_body = {
        "card_id": card["card_id"],
        "item_id": card["item"]["id"],
        "item_version": card["item"]["version"],
        "selected": [1],
        "attempt_token": "api-test-token",
    }
    attempt = client.post("/api/practice/attempts", json=attempt_body)
    assert attempt.status_code == 200
    assert attempt.json()["correct"] is True
    replay = client.post("/api/practice/attempts", json=attempt_body)
    assert replay.status_code == 200
    assert replay.json()["attempt_id"] == attempt.json()["attempt_id"]

    teach = client.post(
        "/api/practice/teach",
        json={"skill_ids": ["api.kin"], "attempt_ids": [], "intent": "fix"},
    )
    assert teach.status_code == 200
    assert teach.json()["thread_id"]

    events = client.post("/api/home/events", json={"events": [{"type": "dwell", "value": {"ms": 100}}]})
    assert events.status_code == 200 and events.json()["accepted"] == 1

    settings = client.post("/api/practice/settings", json={"daily_cards": 8, "timezone": "Asia/Tokyo"})
    assert settings.status_code == 200
    assert settings.json()["settings"]["daily_cards"] == 8

    progress = client.get("/api/practice/progress")
    assert progress.status_code == 200

    reset = client.post("/api/practice/reset", json={"confirm": True})
    assert reset.status_code == 200

    # Clean up global test rows from the shared database.
    main.db.execute("DELETE FROM threads WHERE owner = ?", (session.owner,))
    main.db.execute("DELETE FROM items WHERE owner = ?", (session.owner,))
    main.db.execute("DELETE FROM skills WHERE id = ?", ("api.kin",))


def test_login_is_unavailable_without_the_lms_plugin(monkeypatch):
    import app.main as main

    monkeypatch.setenv("DOOMED_LMS_PLUGIN", "0")
    client = TestClient(main.app)
    response = client.post("/api/login", json={"email": "x@example.com", "password": "nope"})
    assert response.status_code == 503


def test_builtin_lms_plugin_is_optional(monkeypatch):
    import app.integrations as integrations

    monkeypatch.setenv("DOOMED_LMS_PLUGIN", "1")
    statuses = {row["id"]: row for row in integrations.status()}
    assert "indus" in statuses
    assert statuses["indus"]["available"] is True

    local_session = Session(access="", refresh="", tenant="local", uid="student")
    assert integrations.active_plugin(local_session) is None
    context = integrations.build_learning_context(local_session, None, None)
    assert context.connected is False and context.source == "local"

    monkeypatch.setenv("DOOMED_LMS_PLUGIN", "0")
    assert {row["id"]: row for row in integrations.status()}["indus"]["available"] is False


def test_old_database_receives_additive_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
            topic TEXT, subject TEXT, phase TEXT NOT NULL DEFAULT 'probe',
            dependency_map TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE quiz_states (
            call_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, owner TEXT NOT NULL,
            question TEXT NOT NULL, options_json TEXT NOT NULL, correct_json TEXT NOT NULL,
            explanation TEXT, multi_select INTEGER NOT NULL DEFAULT 0,
            answered INTEGER NOT NULL DEFAULT 0, user_answer_json TEXT,
            grade_result_json TEXT, created_at REAL NOT NULL
        );
        """
    )
    conn.close()

    db = Database(path)
    thread_columns = {row["name"] for row in db.query_all("PRAGMA table_info(threads)")}
    quiz_columns = {row["name"] for row in db.query_all("PRAGMA table_info(quiz_states)")}
    assert {"origin", "origin_json"} <= thread_columns
    assert {"skill_ids_json", "item_id", "source"} <= quiz_columns
    assert db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='practice_attempts'")
    db.close()
