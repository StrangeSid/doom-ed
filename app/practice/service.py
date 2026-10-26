# SPDX-License-Identifier: GPL-3.0-or-later
"""Feed planning, attempt grading, progress, and lesson handoff.

This is the only practice module that touches both the store and the thread
database. It does not import an LMS module; optional learning context is supplied
by app.integrations when a plugin is connected.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import date, datetime, time as datetime_time
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.db import Database
from app.practice import config as practice_config
from app.practice.grading import grade_answer, mastery_band, update_state
from app.practice.models import DONT_KNOW, Item, Skill, StudentState
from app.practice.planner import select_cards
from app.practice.store import PracticeStore

_DAY = 86400.0
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _safe_timezone(timezone: str | None) -> str:
    candidate = (timezone or "UTC").strip() or "UTC"
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return candidate


def _move_before(date_value: date, days: int) -> date:
    return date.fromordinal(date_value.toordinal() - days)


class PracticeService:
    """Application service for the home feed and practice state."""

    def __init__(
        self,
        db: Database,
        *,
        store: PracticeStore | None = None,
        config: practice_config.PracticeConfig | None = None,
    ) -> None:
        self.db = db
        self.store = store or PracticeStore(db)
        self.config = config or practice_config.get_config()

    # ------------------------------------------------------------------
    # Plan and feed
    # ------------------------------------------------------------------

    def plan_feed(
        self,
        owner: str,
        *,
        subjects: Iterable[str] | None = None,
        cursor: int = 0,
        limit: int | None = None,
        day: str | None = None,
        timezone: str | None = None,
        target_session: str = "",
        refresh: bool = False,
    ) -> dict[str, Any]:
        settings = self.store.get_owner_settings(owner) or {}
        tz = _safe_timezone(timezone or str(settings.get("timezone") or "UTC"))
        day_value = self._resolve_day(day, tz)
        goal = _clamp(int(settings.get("daily_cards") or self.config.daily_cards), 1, 30)
        page_limit = _clamp(int(limit or self.config.page_limit), 1, self.config.max_page_limit)
        cursor = max(0, int(cursor or 0))

        plan = self.store.get_plan(owner, day_value)
        if plan is None:
            plan = self._create_plan(
                owner=owner,
                day=day_value,
                timezone=tz,
                goal=goal,
                subjects=set(subjects or ()),
            )
        elif self.store.count_cards(str(plan["id"]), owner) == 0 and self.store.count_verified_items(owner):
            # The plan may have been created before the item bank was populated
            # (for example during onboarding). Fill it on the next request.
            plan = self._create_plan(
                owner=owner,
                day=day_value,
                timezone=tz,
                goal=goal,
                subjects=set(subjects or ()),
            )

        total_cards = self.store.count_cards(str(plan["id"]), owner)
        cards = self.store.get_cards(str(plan["id"]), owner, offset=cursor, limit=page_limit)
        item_ids = [str(card["item_id"]) for card in cards if card.get("item_id")]
        items = self.store.get_items_by_ids(item_ids, owner)
        skill_ids = {
            skill_id
            for item in items.values()
            for skill_id in item.skill_ids
        }
        skills = self.store.get_skills_by_ids(skill_ids)
        answered = self.store.count_answered(str(plan["id"]), owner)
        has_more = cursor + len(cards) < total_cards
        return {
            "plan": {
                "id": str(plan["id"]),
                "day": day_value,
                "timezone": tz,
                "goal_cards": int(plan.get("goal_cards") or goal),
                "answered": answered,
                "total_cards": total_cards,
                "streak_days": self._streak(owner, tz),
                "due_reviews": self.store.count_due(owner, time.time()),
                "exam_countdown_days": self._exam_countdown(target_session),
                "checkpoint_due": self._checkpoint_due(owner, settings),
                "needs_calibration": total_cards == 0,
            },
            "cards": [
                self._serialize_card(card, items.get(str(card.get("item_id") or "")), skills)
                for card in cards
            ],
            "next_cursor": cursor + len(cards) if has_more else None,
            "has_more": has_more,
        }

    def _create_plan(
        self,
        *,
        owner: str,
        day: str,
        timezone: str,
        goal: int,
        subjects: set[str],
    ) -> dict[str, Any]:
        items = self.store.list_verified_items(owner)
        skills = {skill.id: skill for skill in self.store.list_skills()}
        states = {state.skill_id: state for state in self.store.list_states(owner)}
        now = time.time()

        day_start = self._day_start(day, timezone)
        recent_attempts = self.store.attempts_since(owner, now - _DAY, limit=500)
        attempts_today = self._count_attempts_by_skill(
            self.store.attempts_since(owner, day_start, limit=500)
        )
        recent_item_ids = {str(row["item_id"]) for row in recent_attempts}
        recent_skill_ids: list[str] = []
        for row in recent_attempts:
            try:
                parsed = json.loads(row.get("skill_ids_json") or "[]")
            except (TypeError, ValueError):
                parsed = []
            recent_skill_ids.extend(str(skill_id) for skill_id in parsed)
        # recent_attempts is newest first. The planner expects chronological
        # order so its last-three window means the most recently seen skills.
        recent_skill_ids.reverse()
        seed = int(hashlib.sha256(f"{owner}:{day}".encode("utf-8")).hexdigest()[:16], 16)

        planned = select_cards(
            owner=owner,
            states=states,
            skills=skills,
            items=items,
            enrolled_subjects=subjects,
            recent_item_ids=recent_item_ids,
            recent_skill_ids=recent_skill_ids,
            attempts_by_skill_today=attempts_today,
            now=now,
            seed=seed,
            goal=goal,
            config=self.config,
        )
        plan = self.store.create_plan(owner, day, timezone, goal)
        if planned:
            self.store.add_cards(str(plan["id"]), owner, planned)
        return self.store.get_plan(owner, day) or plan

    def _serialize_card(
        self,
        card: dict[str, Any],
        item: Item | None,
        skills: dict[str, Skill],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "card_id": str(card["id"]),
            "kind": str(card.get("kind") or "practice"),
            "position": int(card.get("position") or 0),
            "state": str(card.get("state") or "pending"),
            "reason": {
                "code": str(card.get("reason_code") or "practice"),
                "text": str(card.get("reason_text") or "Practice this skill."),
            },
        }
        if item is not None:
            labels = [
                skills[skill_id].title
                for skill_id in item.skill_ids
                if skill_id in skills
            ]
            payload["item"] = {
                "id": item.id,
                "version": item.version,
                "format": item.format,
                "stem": item.stem,
                "options": [
                    {
                        "index": int(option.get("index", index + 1)),
                        "label": str(option.get("label") or ""),
                        "value": option.get("value", option.get("label")),
                        "description": str(option.get("description") or ""),
                    }
                    for index, option in enumerate(item.options)
                ],
                "skill_ids": list(item.skill_ids),
                "skill_labels": labels,
                "difficulty_band": self._difficulty_band(item.difficulty),
                "estimated_seconds": 75 if item.format == "numeric" else 90,
            }
        return payload

    @staticmethod
    def _difficulty_band(difficulty: float) -> str:
        if difficulty <= -0.5:
            return "easy"
        if difficulty <= 0.5:
            return "medium"
        return "hard"

    def _resolve_day(self, day: str | None, timezone: str) -> str:
        if day and _DAY_RE.match(day):
            return day
        return datetime.now(ZoneInfo(timezone)).date().isoformat()

    def _day_start(self, day: str, timezone: str) -> float:
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            parsed = datetime.now(ZoneInfo(timezone)).date()
        start = datetime.combine(parsed, datetime_time.min, tzinfo=ZoneInfo(timezone))
        return start.timestamp()

    @staticmethod
    def _count_attempts_by_skill(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            try:
                skill_ids = json.loads(row.get("skill_ids_json") or "[]")
            except (TypeError, ValueError):
                skill_ids = []
            for skill_id in skill_ids:
                counts[str(skill_id)] = counts.get(str(skill_id), 0) + 1
        return counts

    def _streak(self, owner: str, timezone: str) -> int:
        days = set(self.store.attempt_days(owner, limit=90))
        if not days:
            return 0
        today = datetime.now(ZoneInfo(timezone)).date()
        cursor = today if today.isoformat() in days else _move_before(today, 1)
        streak = 0
        while cursor.isoformat() in days:
            streak += 1
            cursor = _move_before(cursor, 1)
        return streak

    def _exam_countdown(self, target_session: str) -> int | None:
        match = re.search(r"([A-Za-z]+)\s+(\d{4})", target_session or "")
        if not match:
            return None
        month = _MONTHS.get(match.group(1).lower())
        if not month:
            return None
        try:
            exam = date(int(match.group(2)), month, 1)
        except ValueError:
            return None
        return max(0, (exam - date.today()).days)

    def _checkpoint_due(self, owner: str, settings: dict[str, Any]) -> bool:
        if not int(settings.get("checkpoints_enabled", 1)):
            return False
        return any(state.checkpoint_eligible for state in self.store.list_states(owner))

    # ------------------------------------------------------------------
    # Grading
    # ------------------------------------------------------------------

    def grade_attempt(
        self,
        owner: str,
        *,
        card_id: str,
        item_id: str,
        item_version: int,
        selected: Any,
        attempt_token: str,
        confidence: int | None = None,
        latency_ms: int | None = None,
        source: str = "feed",
    ) -> dict[str, Any]:
        token = (attempt_token or "").strip()
        if not token or len(token) > 128:
            raise ValueError("A valid attempt token is required.")
        replay = self.store.get_attempt_by_token(owner, token)
        if replay is not None:
            if str(replay.get("card_id") or "") != str(card_id) or str(replay.get("item_id") or "") != str(item_id):
                raise ConflictError("This attempt token belongs to a different card.")
            try:
                return json.loads(replay.get("result_json") or "{}")
            except (TypeError, ValueError):
                return self._result_from_attempt(replay)

        card = self.store.get_card(card_id, owner)
        if card is None:
            raise ValueError("Unknown practice card.")
        if str(card.get("state")) == "answered":
            raise ConflictError("This card has already been answered.")
        if str(card.get("kind")) not in {"practice", "checkpoint"} or not card.get("item_id"):
            raise ConflictError("This card cannot be graded.")
        if str(card["item_id"]) != str(item_id):
            raise ConflictError("This card does not match the submitted item.")

        item = self.store.get_item(item_id, owner)
        if item is None:
            raise ValueError("Unknown practice item.")
        if int(item.version) != int(item_version):
            raise ConflictError("This item version is no longer current.")

        outcome = grade_answer(item, selected)
        primary_skill = item.primary_skill
        state = self.store.get_state(owner, primary_skill) or StudentState(
            owner=owner,
            skill_id=primary_skill,
            mastery=0.35,
        )
        stats = self.store.get_item_stats(item.id)
        new_state, new_difficulty = update_state(
            state,
            item=item,
            correct=outcome.correct,
            dont_know=outcome.dont_know,
            item_attempts=int(stats.get("attempts") or 0),
            now=time.time(),
            confidence=confidence,
            target_retention=self.config.target_retention,
        )

        follow_up = self._follow_up(owner, item, outcome.correct, outcome.dont_know)
        result = {
            "status": "ok",
            "correct": outcome.correct,
            "dont_know": outcome.dont_know,
            "correct_options": list(outcome.correct_options),
            "explanation": outcome.explanation,
            "mastery": {
                "before": round(state.mastery, 4),
                "after": round(new_state.mastery, 4),
                "band": mastery_band(new_state.mastery),
            },
            "next_due_at": new_state.due_at,
            "follow_up": follow_up,
            "attempt_id": uuid.uuid4().hex,
        }

        attempt = {
            "id": result["attempt_id"],
            "owner": owner,
            "attempt_token": token,
            "item_id": item.id,
            "item_version": item.version,
            "skill_ids": item.skill_ids,
            "selected": list(outcome.selected),
            "correct": outcome.correct,
            "dont_know": outcome.dont_know,
            "latency_ms": _clamp(int(latency_ms or 0), 0, 300000) or None,
            "confidence": confidence,
            "source": source,
            "card_id": card_id,
            "planner_reason": card.get("reason_code"),
            "explanation_shown": True,
            "result": result,
        }
        self.store.commit_attempt(
            attempt=attempt,
            state=new_state,
            item_difficulty=new_difficulty,
            item_correct=outcome.correct,
        )
        return result

    def record_lesson_quiz(
        self,
        owner: str,
        *,
        thread_id: str,
        card_id: str,
        quiz_state: dict[str, Any],
        selected: Any,
    ) -> dict[str, Any]:
        """Apply one lesson quiz answer to the same skill state as the feed.

        The teacher's quiz lives in quiz_states, not items. We build an in-memory
        item from that state so grading and mastery use one code path. If the
        teacher omitted skill_ids, the attempt is deliberately left unmapped
        instead of guessing which skill it belongs to.
        """
        try:
            skill_ids = tuple(
                str(skill_id)
                for skill_id in json.loads(quiz_state.get("skill_ids_json") or "[]")
                if skill_id
            )
        except (TypeError, ValueError):
            skill_ids = ()
        if not skill_ids:
            return {"status": "unmapped", "reason": "The teacher did not attach skill_ids."}

        token = f"lesson:{card_id}"
        replay = self.store.get_attempt_by_token(owner, token)
        if replay is not None:
            try:
                return json.loads(replay.get("result_json") or "{}")
            except (TypeError, ValueError):
                return self._result_from_attempt(replay)

        try:
            options = json.loads(quiz_state.get("options_json") or "[]")
            correct = json.loads(quiz_state.get("correct_json") or "[]")
        except (TypeError, ValueError):
            options, correct = [], []
        item = Item(
            id=f"lesson:{card_id}",
            skill_ids=skill_ids,
            format="mcq_multi" if int(quiz_state.get("multi_select") or 0) else "mcq_single",
            stem=str(quiz_state.get("question") or ""),
            options=tuple(option for option in options if isinstance(option, dict)),
            solution={
                "correct": correct if isinstance(correct, list) else [correct],
                "explanation": str(quiz_state.get("explanation") or ""),
            },
            owner=owner,
            verification="verified",
            content_hash=f"lesson-{card_id}",
        )
        outcome = grade_answer(item, selected)
        primary_skill = item.primary_skill
        state = self.store.get_state(owner, primary_skill) or StudentState(
            owner=owner,
            skill_id=primary_skill,
            mastery=0.35,
        )
        stats = self.store.get_item_stats(item.id)
        new_state, new_difficulty = update_state(
            state,
            item=item,
            correct=outcome.correct,
            dont_know=outcome.dont_know,
            item_attempts=int(stats.get("attempts") or 0),
            now=time.time(),
            target_retention=self.config.target_retention,
        )
        result = {
            "status": "ok",
            "source": "lesson",
            "correct": outcome.correct,
            "dont_know": outcome.dont_know,
            "correct_options": list(outcome.correct_options),
            "explanation": outcome.explanation,
            "mastery": {
                "before": round(state.mastery, 4),
                "after": round(new_state.mastery, 4),
                "band": mastery_band(new_state.mastery),
            },
            "next_due_at": new_state.due_at,
            "attempt_id": uuid.uuid4().hex,
        }
        attempt = {
            "id": result["attempt_id"],
            "owner": owner,
            "attempt_token": token,
            "item_id": item.id,
            "item_version": 1,
            "skill_ids": item.skill_ids,
            "selected": list(outcome.selected),
            "correct": outcome.correct,
            "dont_know": outcome.dont_know,
            "source": "lesson",
            "thread_id": thread_id,
            "card_id": card_id,
            "explanation_shown": True,
            "result": result,
        }
        self.store.commit_attempt(
            attempt=attempt,
            state=new_state,
            item_difficulty=new_difficulty,
            item_correct=outcome.correct,
        )
        return result

    def _follow_up(self, owner: str, item: Item, correct: bool, dont_know: bool) -> dict[str, Any] | None:
        if correct:
            return None
        recent = self.store.recent_skill_attempts(owner, item.skill_ids, limit=2)
        prior_misses = sum(1 for row in recent if not row.get("correct"))
        if dont_know or prior_misses >= 1:
            return {
                "kind": "teach",
                "skill_ids": list(item.skill_ids),
                "reason": "Two checks in a row did not land. A short lesson should come before more practice.",
            }
        return {
            "kind": "repair",
            "skill_ids": list(item.skill_ids),
            "reason": "A short scaffold and one easier check are queued for this skill.",
        }

    @staticmethod
    def _result_from_attempt(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "ok",
            "correct": bool(row.get("correct")),
            "dont_know": bool(row.get("dont_know")),
            "correct_options": [],
            "explanation": "",
            "mastery": {},
            "next_due_at": None,
            "follow_up": None,
            "attempt_id": row.get("id"),
        }

    # ------------------------------------------------------------------
    # Progress, handoff, settings, events
    # ------------------------------------------------------------------

    def progress(self, owner: str) -> dict[str, Any]:
        skills = {skill.id: skill for skill in self.store.list_skills()}
        states = self.store.list_states(owner)
        subjects: dict[str, dict[str, Any]] = {}
        for state in states:
            skill = skills.get(state.skill_id)
            if skill is None:
                continue
            bucket = subjects.setdefault(
                skill.subject,
                {"subject": skill.subject, "skills": [], "due": 0, "attempts": 0},
            )
            bucket["skills"].append(
                {
                    "id": skill.id,
                    "title": skill.title,
                    "unit": skill.unit,
                    "mastery": round(state.mastery, 4),
                    "band": mastery_band(state.mastery),
                    "attempts": state.attempts,
                    "due_at": state.due_at,
                    "checkpoint_eligible": state.checkpoint_eligible,
                }
            )
            bucket["attempts"] += state.attempts
            if state.due_at is not None and state.due_at <= time.time():
                bucket["due"] += 1
        return {
            "subjects": list(subjects.values()),
            "streak_days": self._streak(owner, "UTC"),
            "due_reviews": self.store.count_due(owner, time.time()),
            "recent_attempts": len(self.store.recent_attempts(owner, limit=100)),
        }

    def teach(
        self,
        owner: str,
        *,
        skill_ids: Iterable[str],
        attempt_ids: Iterable[str] | None = None,
        intent: str = "teach",
    ) -> dict[str, Any]:
        ids = [str(skill_id) for skill_id in skill_ids if skill_id]
        if not ids:
            raise ValueError("At least one skill is required.")
        brief = self.store.evidence_brief(owner, ids, attempt_ids or [])
        skills = self.store.get_skills_by_ids(ids)
        titles = [skills[skill_id].title for skill_id in ids if skill_id in skills]
        subject = next((skills[skill_id].subject for skill_id in ids if skill_id in skills), "")
        title = "Practise: " + (", ".join(titles)[:60] if titles else "skill")
        brief["intent"] = str(intent or "teach")
        thread = self.db.create_thread(
            owner=owner,
            title=title,
            topic="practice",
            subject=subject or None,
            origin="practice",
            origin_json=brief,
        )
        seed = (
            "I need to fix " + (", ".join(titles) if titles else "this skill") + ". "
            "Start from the foundations and use my recent practice evidence."
        )
        return {
            "status": "ok",
            "thread_id": thread.get("id"),
            "seed_message": seed,
            "brief_summary": {
                "skills": len(brief.get("skills") or []),
                "attempts": len(brief.get("attempts") or []),
            },
        }

    def add_events(self, owner: str, events: list[dict[str, Any]]) -> int:
        allowed = {
            "impression",
            "dwell",
            "skip",
            "reveal",
            "bookmark",
            "report",
            "teach_click",
            "checkpoint_start",
            "checkpoint_submit",
            "feed_end",
            "reflection",
        }
        clean: list[dict[str, Any]] = []
        for event in events[:50]:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "").strip()
            if event_type not in allowed:
                continue
            value = event.get("value")
            if value is not None and not isinstance(value, (dict, list, str, int, float, bool)):
                continue
            if event_type == "dwell" and isinstance(value, dict):
                try:
                    value = {**value, "ms": max(0, min(600000, int(value.get("ms") or 0)))}
                except (TypeError, ValueError):
                    continue
            if value is not None:
                try:
                    if len(json.dumps(value, default=str)) > 8192:
                        continue
                except (TypeError, ValueError):
                    continue
            clean.append(
                {
                    "type": event_type,
                    "card_id": event.get("card_id"),
                    "plan_id": event.get("plan_id"),
                    "value": value,
                }
            )
        return self.store.add_events(owner, clean)

    def update_settings(self, owner: str, **fields: Any) -> dict[str, Any]:
        if "daily_cards" in fields and fields["daily_cards"] is not None:
            fields["daily_cards"] = _clamp(int(fields["daily_cards"]), 1, 30)
        if "target_retention" in fields and fields["target_retention"] is not None:
            fields["target_retention"] = max(0.80, min(0.95, float(fields["target_retention"])))
        if "timezone" in fields and fields["timezone"] is not None:
            fields["timezone"] = _safe_timezone(str(fields["timezone"]))
        if "checkpoints_enabled" in fields and fields["checkpoints_enabled"] is not None:
            fields["checkpoints_enabled"] = 1 if fields["checkpoints_enabled"] else 0
        settings = self.store.upsert_owner_settings(owner, **fields)
        return {
            "timezone": settings.get("timezone"),
            "daily_cards": settings.get("daily_cards"),
            "target_retention": settings.get("target_retention"),
            "checkpoints_enabled": bool(settings.get("checkpoints_enabled")),
            "difficulty_pref": settings.get("difficulty_pref"),
        }

    def report_item(self, owner: str, item_id: str, kind: str, note: str = "") -> dict[str, Any]:
        allowed = {"ambiguous", "wrong", "outdated", "too_easy", "too_hard"}
        if kind not in allowed:
            raise ValueError("Unknown report type.")
        item = self.store.get_item(item_id, owner)
        if item is None:
            raise ValueError("Unknown practice item.")
        report = self.store.record_item_report(owner, item_id, kind, note[:500])
        return {"status": "ok", "report_id": report.get("id")}

    def reset(self, owner: str, *, confirm: bool) -> dict[str, Any]:
        if not confirm:
            raise ValueError("Reset requires explicit confirmation.")
        return {"status": "ok", "deleted": self.store.reset_practice(owner)}


class ConflictError(Exception):
    """Raised when an item version or card state makes the request stale."""
