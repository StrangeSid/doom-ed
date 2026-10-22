# SPDX-License-Identifier: GPL-3.0-or-later
"""Orchestrator regression tests: quiz state persistence, card reply grading,
tool-result round trip, and the visual maker publish path."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.db import Database
from app.orchestrator import Orchestrator
from app.security import WriteConfirmationManager
from app.sessions import Session


@pytest.fixture
def orch(tmp_path):
    db = Database(tmp_path / "doomed.sqlite")
    wm = WriteConfirmationManager()
    o = Orchestrator(db=db, write_mgr=wm)
    o._db = db
    yield o
    db.close()


@pytest.fixture
def session():
    return Session(
        access="tok",
        refresh="ref",
        tenant="tenant_x",
        name="Test Student",
        email="t@school.edu",
        uid="u_test",
        grade="Grade 11",
    )


def _quiz_response(message: dict) -> dict:
    """Extract a tool call for `quiz` from a fake LLM response."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": message.get("content", ""),
                    "tool_calls": message.get("tool_calls", []),
                }
            }
        ]
    }


class _FakeStream:
    """Minimal stand-in for a streamed OpenAI-compatible chat response."""

    def __init__(self, content: str = "", tool_calls: list[dict] | None = None):
        self._content = content
        self._tool_calls = tool_calls or []

    def iter_lines(self, decode_unicode: bool = True):
        if self._content:
            yield f'data: {json.dumps({"choices": [{"delta": {"content": self._content}}]})}'
        for tc in self._tool_calls:
            fn = tc["function"]
            chunk = {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": tc["id"],
                                    "type": "function",
                                    "function": {"name": fn["name"], "arguments": fn["arguments"]},
                                }
                            ]
                        }
                    }
                ]
            }
            yield f'data: {json.dumps(chunk)}'
        yield "data: [DONE]"


def _fake_llm(content: str = "", tool_calls: list[dict] | None = None):
    """Return the right shape for streaming and non-streaming calls alike."""
    def _side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            return _FakeStream(content=content, tool_calls=tool_calls)
        return _quiz_response({"content": content, "tool_calls": tool_calls or []})

    return _side_effect


def test_quiz_state_persists_and_grades_correctly(orch, session):
    """A quiz tool call must persist state and grade the student's answer."""
    tool_call = {
        "id": "call_q1",
        "type": "function",
        "function": {
            "name": "quiz",
            "arguments": json.dumps(
                {
                    "question": "What is 2 + 2?",
                    "options": [
                        {"label": "3", "value": "3"},
                        {"label": "4", "value": "4"},
                        {"label": "5", "value": "5"},
                    ],
                    "correct": 1,
                    "explanation": "Basic arithmetic.",
                }
            ),
        },
    }

    with patch("app.orchestrator.call_llm_chat", side_effect=_fake_llm(tool_calls=[tool_call])):
        events = list(
            orch.process_user_turn(
                session=session,
                thread_id="t_quiz",
                user_message="Teach me addition.",
            )
        )

    card_events = [e for e in events if e["event"] == "card"]
    assert len(card_events) == 1
    card = card_events[0]["data"]
    assert card["card_type"] == "quiz"
    assert card["call_id"] == "call_q1"

    # The server sends only the real options, indexed 1..N. The client prepends
    # the pinned "I don't know" option at display index 0 (cards.js).
    assert len(card["options"]) == 3
    assert sorted(o["index"] for o in card["options"]) == [1, 2, 3]

    # The correct answer is held server-side, never in the card payload.
    assert "correct" not in card
    assert "explanation" not in card

    state = orch.db.get_quiz_state("call_q1", owner=session.owner)
    assert state is not None
    correct_indices = json.loads(state["correct_json"])
    assert len(correct_indices) == 1
    correct_index = correct_indices[0]
    assert correct_index != 0, "the pinned don't-know option can never be correct"

    # Grading a correct answer
    reply = orch.submit_card_reply(
        session=session,
        thread_id="t_quiz",
        card_id="call_q1",
        reply_data={"selected": [correct_index]},
    )
    assert reply["status"] == "ok"
    assert reply["grade"]["correct"] is True
    assert reply["grade"]["dontKnow"] is False

    # Grading the pinned "I don't know"
    reply_dk = orch.submit_card_reply(
        session=session,
        thread_id="t_quiz",
        card_id="call_q1",
        reply_data={"selected": [0]},
    )
    assert reply_dk["grade"]["dontKnow"] is True
    assert reply_dk["grade"]["correct"] is False


def test_quiz_state_is_owner_scoped(orch, session):
    """Another student must not be able to read or grade this quiz."""
    tool_call = {
        "id": "call_q2",
        "type": "function",
        "function": {
            "name": "quiz",
            "arguments": json.dumps(
                {
                    "question": "Pick one",
                    "options": [{"label": "A", "value": "a"}, {"label": "B", "value": "b"}],
                    "correct": 0,
                    "explanation": "Because.",
                }
            ),
        },
    }
    with patch("app.orchestrator.call_llm_chat", side_effect=_fake_llm(tool_calls=[tool_call])):
        list(orch.process_user_turn(session=session, thread_id="t_owner", user_message="go"))

    intruder = Session(
        access="tok2",
        refresh="ref2",
        tenant="tenant_x",
        name="Other",
        email="o@school.edu",
        uid="u_other",
    )
    assert orch.db.get_quiz_state("call_q2", owner=intruder.owner) is None


def test_ask_card_records_answer(orch, session):
    tool_call = {
        "id": "call_a1",
        "type": "function",
        "function": {
            "name": "ask_user_question",
            "arguments": json.dumps(
                {
                    "question": "What do you want to learn?",
                    "mode": "single-select",
                    "options": [{"label": "Kinematics", "value": "kinematics"}],
                }
            ),
        },
    }
    with patch("app.orchestrator.call_llm_chat", side_effect=_fake_llm(tool_calls=[tool_call])):
        events = list(
            orch.process_user_turn(session=session, thread_id="t_ask", user_message="plan")
        )

    card = [e for e in events if e["event"] == "card"][0]["data"]
    assert card["card_type"] == "ask"

    reply = orch.submit_card_reply(
        session=session,
        thread_id="t_ask",
        card_id="call_a1",
        reply_data={"answer": "kinematics"},
    )
    assert reply["status"] == "ok"
    assert reply["answer"] == "kinematics"

    msgs = orch.db.list_messages("t_ask")
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert any("kinematics" in (m["content"] or "") for m in tool_msgs)


def test_visual_publish_saves_record(orch, session):
    """A verified visual must persist to db and report a PNG URL."""
    orch._pending_viz["call_v1"] = {
        "thread_id": "t_viz",
        "owner": session.owner,
        "kind": "mermaid",
        "slug": "vector-triangle",
        "brief": "Show the triangle",
        "source_code": "graph TD\n A-->B",
        "iteration": 1,
        "maker_messages": [],
    }

    res = orch.handle_viz_feedback(
        session,
        "call_v1",
        {"ok": True, "dom_warnings": [], "png_data_url": None},
    )
    assert res["status"] == "published"
    assert res["png_url"].startswith("/api/viz/")

    visual = orch.db.get_visual(res["visual_id"])
    assert visual is not None
    assert visual["slug"] == "vector-triangle"
    assert visual["owner"] == session.owner
    assert visual["verified"] == 1


def test_visual_retry_loop_returns_revised_source(orch, session):
    """DOM warnings must trigger a maker revision, not a publish."""
    orch._pending_viz["call_v2"] = {
        "thread_id": "t_viz2",
        "owner": session.owner,
        "kind": "svg",
        "slug": "triangle",
        "brief": "Geometry",
        "source_code": "<svg></svg>",
        "iteration": 1,
        "maker_messages": [],
    }

    with patch(
        "app.orchestrator.call_llm_chat",
        side_effect=_fake_llm(content="```svg\n<svg><rect/></svg>\n```"),
    ):
        res = orch.handle_viz_feedback(
            session,
            "call_v2",
            {"ok": True, "dom_warnings": ["labels overlap"], "png_data_url": None},
        )

    assert res["status"] == "retry_render"
    assert "rect" in res["source"]


def test_multi_turn_tool_execution(orch, session):
    """When a tool is called, orchestrator must pass assistant message and tool result to turn 2."""
    tool_call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "search_ib_resources", "arguments": "{}"},
    }
    calls = []

    def mock_llm(*a, **kw):
        calls.append(list(kw.get("messages", [])))
        if len(calls) == 1:
            return _FakeStream(tool_calls=[tool_call])
        return _FakeStream(content="done")

    with patch("app.orchestrator.call_llm_chat", side_effect=mock_llm), \
         patch("app.ib_client.search_ib", return_value=[]):
        list(orch.process_user_turn(session=session, thread_id="t_m", user_message="find"))

    assert len(calls) == 2
    roles = [m["role"] for m in calls[1]]
    assert "assistant" in roles and "tool" in roles
    asst_idx, tool_idx = roles.index("assistant"), roles.index("tool")
    assert asst_idx < tool_idx
    assert calls[1][asst_idx]["tool_calls"] == [tool_call]
    assert calls[1][tool_idx]["tool_call_id"] == "c1"


