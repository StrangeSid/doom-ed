# SPDX-License-Identifier: GPL-3.0-or-later
"""Owner-scoped persistence for the adaptive practice engine.

All SQL lives here. The service layer never builds queries. Every student table
is filtered by owner; items may be ownerless only when they are shared and
verified.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable

from app.db import Database
from app.practice.models import Item, PlannedCard, Skill, StudentState
from app.security import Cipher


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _skill_from_row(row: dict[str, Any]) -> Skill:
    return Skill(
        id=str(row["id"]),
        subject=str(row["subject"] or ""),
        title=str(row["title"] or ""),
        level=str(row["level"] or ""),
        unit=str(row["unit"] or ""),
        description=str(row["description"] or ""),
        ib_code=str(row["ib_code"] or ""),
        prereq_ids=tuple(str(x) for x in _loads(row.get("prereq_ids_json"), [])),
        exam_weight=float(row.get("exam_weight") or 1.0),
        source_kind=str(row.get("source_kind") or ""),
        source_ref=str(row.get("source_ref") or ""),
    )


def _state_from_row(row: dict[str, Any]) -> StudentState:
    return StudentState(
        owner=str(row["owner"]),
        skill_id=str(row["skill_id"]),
        ability=float(row.get("ability") or 0.0),
        mastery=float(row.get("mastery") or 0.35),
        attempts=int(row.get("attempts") or 0),
        correct=int(row.get("correct") or 0),
        unknowns=int(row.get("unknowns") or 0),
        streak=int(row.get("streak") or 0),
        stability=float(row.get("stability") or 1.0),
        due_at=float(row["due_at"]) if row.get("due_at") is not None else None,
        last_seen=float(row["last_seen"]) if row.get("last_seen") is not None else None,
        checkpoint_eligible=bool(row.get("checkpoint_eligible")),
    )


class PracticeStore:
    """SQLite access for skills, items, attempts, feed plans, and settings."""

    def __init__(self, db: Database, *, cipher: Cipher | None = None) -> None:
        self.db = db
        self.cipher = cipher or Cipher()

    # ------------------------------------------------------------------
    # Skills and items
    # ------------------------------------------------------------------

    def upsert_skill(self, skill: Skill) -> Skill:
        now = time.time()
        self.db.execute(
            """INSERT INTO skills (
                   id, subject, level, unit, title, description, ib_code,
                   prereq_ids_json, exam_weight, source_kind, source_ref,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   subject = excluded.subject,
                   level = excluded.level,
                   unit = excluded.unit,
                   title = excluded.title,
                   description = excluded.description,
                   ib_code = excluded.ib_code,
                   prereq_ids_json = excluded.prereq_ids_json,
                   exam_weight = excluded.exam_weight,
                   source_kind = excluded.source_kind,
                   source_ref = excluded.source_ref,
                   updated_at = excluded.updated_at""",
            (
                skill.id,
                skill.subject,
                skill.level,
                skill.unit,
                skill.title,
                skill.description,
                skill.ib_code,
                _dumps(list(skill.prereq_ids)),
                float(skill.exam_weight),
                skill.source_kind,
                skill.source_ref,
                now,
                now,
            ),
        )
        return skill

    def get_skill(self, skill_id: str) -> Skill | None:
        row = self.db.query_one("SELECT * FROM skills WHERE id = ?", (skill_id,))
        return _skill_from_row(row) if row else None

    def list_skills(self, *, subject: str | None = None) -> list[Skill]:
        if subject:
            rows = self.db.query_all(
                "SELECT * FROM skills WHERE subject = ? ORDER BY unit, title",
                (subject,),
            )
        else:
            rows = self.db.query_all("SELECT * FROM skills ORDER BY subject, unit, title")
        return [_skill_from_row(row) for row in rows]

    def get_skills_by_ids(self, skill_ids: Iterable[str]) -> dict[str, Skill]:
        ids = [str(skill_id) for skill_id in skill_ids if skill_id]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.query_all(
            f"SELECT * FROM skills WHERE id IN ({placeholders})",
            tuple(ids),
        )
        return {str(row["id"]): _skill_from_row(row) for row in rows}

    def find_item_by_content_hash(self, content_hash: str, owner: str | None) -> Item | None:
        if not content_hash:
            return None
        row = self.db.query_one(
            "SELECT * FROM items WHERE content_hash = ? AND (owner IS ? OR owner = ?) LIMIT 1",
            (content_hash, owner, owner),
        )
        return self._item_from_row(row) if row else None

    def insert_item(self, item: Item) -> Item:
        existing = self.find_item_by_content_hash(item.content_hash, item.owner)
        if existing is not None:
            return existing
        now = time.time()
        solution_enc = self.cipher.encrypt_bytes(_dumps(item.solution).encode("utf-8"))
        self.db.execute(
            """INSERT OR IGNORE INTO items (
                   id, owner, version, skill_ids_json, format, stem, options_json,
                   solution_enc, difficulty, discrimination, guessing, source_kind,
                   source_ref, source_sha256, verification, model, prompt_version,
                   content_hash, created_at, updated_at, retired_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item.id,
                item.owner,
                int(item.version),
                _dumps(list(item.skill_ids)),
                item.format,
                item.stem,
                _dumps(list(item.options)),
                solution_enc,
                float(item.difficulty),
                float(item.discrimination),
                float(item.guessing),
                item.source_kind,
                item.source_ref,
                item.source_sha256,
                item.verification,
                None,
                None,
                item.content_hash,
                now,
                now,
                item.retired_at,
            ),
        )
        return self.get_item(item.id, item.owner or "") or item

    def _item_from_row(self, row: dict[str, Any]) -> Item:
        solution_raw = self.cipher.decrypt_bytes(row["solution_enc"])
        solution = _loads(solution_raw.decode("utf-8"), {})
        options = tuple(_loads(row.get("options_json"), []))
        skill_ids = tuple(str(x) for x in _loads(row.get("skill_ids_json"), []))
        return Item(
            id=str(row["id"]),
            skill_ids=skill_ids,
            format=str(row["format"]),
            stem=str(row["stem"]),
            options=options,
            solution=solution,
            difficulty=float(row.get("difficulty") or 0.0),
            discrimination=float(row.get("discrimination") or 1.0),
            guessing=float(row.get("guessing") or 0.25),
            owner=row.get("owner"),
            version=int(row.get("version") or 1),
            source_kind=str(row.get("source_kind") or ""),
            source_ref=str(row.get("source_ref") or ""),
            source_sha256=str(row.get("source_sha256") or ""),
            verification=str(row.get("verification") or "draft"),
            content_hash=str(row.get("content_hash") or ""),
            retired_at=float(row["retired_at"]) if row.get("retired_at") is not None else None,
        )

    def get_item(self, item_id: str, owner: str = "") -> Item | None:
        row = self.db.query_one(
            "SELECT * FROM items WHERE id = ? AND (owner IS NULL OR owner = ?)",
            (item_id, owner),
        )
        return self._item_from_row(row) if row else None

    def get_items_by_ids(self, item_ids: Iterable[str], owner: str) -> dict[str, Item]:
        ids = [str(item_id) for item_id in item_ids if item_id]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.query_all(
            f"""SELECT * FROM items
                WHERE id IN ({placeholders}) AND (owner IS NULL OR owner = ?)""",
            tuple(ids) + (owner,),
        )
        return {str(row["id"]): self._item_from_row(row) for row in rows}

    def list_verified_items(self, owner: str, limit: int = 500) -> list[Item]:
        rows = self.db.query_all(
            """SELECT * FROM items
               WHERE verification = 'verified' AND retired_at IS NULL
                 AND (owner IS NULL OR owner = ?)
               ORDER BY created_at DESC
               LIMIT ?""",
            (owner, int(limit)),
        )
        return [self._item_from_row(row) for row in rows]

    def count_verified_items(self, owner: str) -> int:
        row = self.db.query_one(
            """SELECT COUNT(*) AS n FROM items
               WHERE verification = 'verified' AND retired_at IS NULL
                 AND (owner IS NULL OR owner = ?)""",
            (owner,),
        )
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------
    # Student state and attempts
    # ------------------------------------------------------------------

    def get_state(self, owner: str, skill_id: str) -> StudentState | None:
        row = self.db.query_one(
            "SELECT * FROM student_skill_state WHERE owner = ? AND skill_id = ?",
            (owner, skill_id),
        )
        return _state_from_row(row) if row else None

    def list_states(self, owner: str) -> list[StudentState]:
        rows = self.db.query_all(
            "SELECT * FROM student_skill_state WHERE owner = ? ORDER BY due_at",
            (owner,),
        )
        return [_state_from_row(row) for row in rows]

    def upsert_state(self, state: StudentState) -> None:
        with self.db.transaction() as conn:
            self._upsert_state_conn(conn, state, time.time())

    def _upsert_state_conn(self, conn: Any, state: StudentState, now: float) -> None:
        conn.execute(
            """INSERT INTO student_skill_state (
                   owner, skill_id, ability, mastery, attempts, correct, unknowns,
                   streak, stability, due_at, last_seen, checkpoint_eligible, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner, skill_id) DO UPDATE SET
                   ability = excluded.ability,
                   mastery = excluded.mastery,
                   attempts = excluded.attempts,
                   correct = excluded.correct,
                   unknowns = excluded.unknowns,
                   streak = excluded.streak,
                   stability = excluded.stability,
                   due_at = excluded.due_at,
                   last_seen = excluded.last_seen,
                   checkpoint_eligible = excluded.checkpoint_eligible,
                   updated_at = excluded.updated_at""",
            (
                state.owner,
                state.skill_id,
                state.ability,
                state.mastery,
                state.attempts,
                state.correct,
                state.unknowns,
                state.streak,
                state.stability,
                state.due_at,
                state.last_seen,
                1 if state.checkpoint_eligible else 0,
                now,
            ),
        )

    def attempts_since(self, owner: str, since: float, limit: int = 500) -> list[dict[str, Any]]:
        return self.db.query_all(
            """SELECT * FROM practice_attempts
               WHERE owner = ? AND created_at >= ?
               ORDER BY created_at DESC LIMIT ?""",
            (owner, since, int(limit)),
        )

    def recent_attempts(self, owner: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.db.query_all(
            "SELECT * FROM practice_attempts WHERE owner = ? ORDER BY created_at DESC LIMIT ?",
            (owner, int(limit)),
        )

    def attempts_by_ids(self, owner: str, attempt_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = [str(attempt_id) for attempt_id in attempt_ids if attempt_id]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return self.db.query_all(
            f"SELECT * FROM practice_attempts WHERE owner = ? AND id IN ({placeholders})",
            (owner, *ids),
        )

    def recent_skill_attempts(self, owner: str, skill_ids: Iterable[str], limit: int = 5) -> list[dict[str, Any]]:
        ids = [str(skill_id) for skill_id in skill_ids if skill_id]
        if not ids:
            return []
        clauses = " OR ".join("skill_ids_json LIKE ?" for _ in ids)
        params: list[Any] = [owner, *[f'%{skill_id}%' for skill_id in ids], int(limit)]
        return self.db.query_all(
            f"""SELECT * FROM practice_attempts
                WHERE owner = ? AND ({clauses})
                ORDER BY created_at DESC LIMIT ?""",
            tuple(params),
        )

    def get_attempt_by_token(self, owner: str, token: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM practice_attempts WHERE owner = ? AND attempt_token = ?",
            (owner, token),
        )

    def get_attempt(self, owner: str, attempt_id: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM practice_attempts WHERE owner = ? AND id = ?",
            (owner, attempt_id),
        )

    def get_item_stats(self, item_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM item_stats WHERE item_id = ?", (item_id,))
        return row or {"item_id": item_id, "attempts": 0, "correct": 0}

    def commit_attempt(
        self,
        *,
        attempt: dict[str, Any],
        state: StudentState,
        item_difficulty: float,
        item_correct: bool,
    ) -> dict[str, Any]:
        """Write the attempt, state, card state, and item calibration atomically."""
        now = time.time()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO practice_attempts (
                       id, owner, attempt_token, item_id, item_version,
                       skill_ids_json, selected_json, correct, dont_know,
                       latency_ms, confidence, source, thread_id, card_id,
                       explanation_shown, planner_reason, result_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(owner, attempt_token) DO NOTHING""",
                (
                    attempt["id"],
                    attempt["owner"],
                    attempt["attempt_token"],
                    attempt["item_id"],
                    int(attempt["item_version"]),
                    _dumps(list(attempt["skill_ids"])),
                    _dumps(attempt.get("selected")),
                    1 if attempt["correct"] else 0,
                    1 if attempt.get("dont_know") else 0,
                    attempt.get("latency_ms"),
                    attempt.get("confidence"),
                    attempt.get("source") or "feed",
                    attempt.get("thread_id"),
                    attempt.get("card_id"),
                    1 if attempt.get("explanation_shown") else 0,
                    attempt.get("planner_reason"),
                    _dumps(attempt.get("result")),
                    now,
                ),
            )
            self._upsert_state_conn(conn, state, now)
            if attempt.get("card_id"):
                conn.execute(
                    """UPDATE feed_cards
                       SET state = 'answered', attempt_id = ?, updated_at = ?
                       WHERE id = ? AND owner = ?""",
                    (attempt["id"], now, attempt["card_id"], attempt["owner"]),
                )
            conn.execute(
                """INSERT INTO item_stats (item_id, attempts, correct, updated_at)
                   VALUES (?, 1, ?, ?)
                   ON CONFLICT(item_id) DO UPDATE SET
                       attempts = item_stats.attempts + 1,
                       correct = item_stats.correct + ?,
                       updated_at = excluded.updated_at""",
                (attempt["item_id"], 1 if item_correct else 0, now, 1 if item_correct else 0),
            )
            conn.execute(
                "UPDATE items SET difficulty = ?, updated_at = ? WHERE id = ?",
                (float(item_difficulty), now, attempt["item_id"]),
            )
        return self.get_attempt_by_token(attempt["owner"], attempt["attempt_token"]) or {}

    # ------------------------------------------------------------------
    # Feed plans and cards
    # ------------------------------------------------------------------

    def get_plan(self, owner: str, day: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM feed_plans WHERE owner = ? AND day = ?",
            (owner, day),
        )

    def create_plan(self, owner: str, day: str, timezone: str, goal_cards: int) -> dict[str, Any]:
        now = time.time()
        self.db.execute(
            """INSERT OR IGNORE INTO feed_plans (
                   id, owner, day, timezone, goal_cards, state, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
            (uuid.uuid4().hex, owner, day, timezone, int(goal_cards), now, now),
        )
        plan = self.get_plan(owner, day)
        if plan is None:
            raise RuntimeError("Could not create a feed plan.")
        return plan

    def add_cards(self, plan_id: str, owner: str, cards: list[PlannedCard], start_position: int = 0) -> None:
        now = time.time()
        with self.db.transaction() as conn:
            for offset, card in enumerate(cards):
                conn.execute(
                    """INSERT OR IGNORE INTO feed_cards (
                           id, plan_id, owner, position, kind, item_id,
                           checkpoint_id, skill_ids_json, reason_code, reason_text,
                           state, attempt_id, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'pending', NULL, ?, ?)""",
                    (
                        uuid.uuid4().hex,
                        plan_id,
                        owner,
                        int(start_position) + offset,
                        card.kind,
                        card.item_id,
                        _dumps(list(card.skill_ids)),
                        card.reason_code,
                        card.reason_text,
                        now,
                        now,
                    ),
                )

    def get_cards(self, plan_id: str, owner: str, offset: int = 0, limit: int = 10) -> list[dict[str, Any]]:
        return self.db.query_all(
            """SELECT * FROM feed_cards
               WHERE plan_id = ? AND owner = ?
               ORDER BY position ASC LIMIT ? OFFSET ?""",
            (plan_id, owner, int(limit), int(offset)),
        )

    def get_card(self, card_id: str, owner: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM feed_cards WHERE id = ? AND owner = ?",
            (card_id, owner),
        )

    def count_cards(self, plan_id: str, owner: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM feed_cards WHERE plan_id = ? AND owner = ?",
            (plan_id, owner),
        )
        return int(row["n"]) if row else 0

    def count_answered(self, plan_id: str, owner: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM feed_cards WHERE plan_id = ? AND owner = ? AND state = 'answered'",
            (plan_id, owner),
        )
        return int(row["n"]) if row else 0

    def latest_card_positions(self, owner: str, limit: int = 5) -> list[dict[str, Any]]:
        return self.db.query_all(
            "SELECT * FROM feed_cards WHERE owner = ? ORDER BY updated_at DESC, position DESC LIMIT ?",
            (owner, int(limit)),
        )

    def add_events(self, owner: str, events: list[dict[str, Any]]) -> int:
        if not events:
            return 0
        now = time.time()
        with self.db.transaction() as conn:
            for event in events:
                conn.execute(
                    """INSERT INTO feed_events (
                           id, owner, plan_id, card_id, event_type, value_json, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        uuid.uuid4().hex,
                        owner,
                        event.get("plan_id"),
                        event.get("card_id"),
                        event.get("type"),
                        _dumps(event.get("value")) if event.get("value") is not None else None,
                        now,
                    ),
                )
        return len(events)

    def attempt_days(self, owner: str, limit: int = 60) -> list[str]:
        rows = self.db.query_all(
            """SELECT DISTINCT date(created_at, 'unixepoch') AS day
               FROM practice_attempts WHERE owner = ?
               ORDER BY day DESC LIMIT ?""",
            (owner, int(limit)),
        )
        return [str(row["day"]) for row in rows if row.get("day")]

    def count_due(self, owner: str, now: float) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM student_skill_state WHERE owner = ? AND due_at IS NOT NULL AND due_at <= ?",
            (owner, now),
        )
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------
    # Settings, reports, reset
    # ------------------------------------------------------------------

    def get_owner_settings(self, owner: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM owner_settings WHERE owner = ?", (owner,))

    def upsert_owner_settings(self, owner: str, **fields: Any) -> dict[str, Any]:
        current = self.get_owner_settings(owner) or {}
        focus_value = fields.get("focus_subjects")
        if focus_value is None:
            focus_json = current.get("focus_subjects_json") or "[]"
        elif isinstance(focus_value, str):
            focus_json = focus_value
        else:
            focus_json = _dumps(focus_value)
        merged = {
            "timezone": fields.get("timezone", current.get("timezone", "UTC")),
            "daily_cards": int(fields.get("daily_cards", current.get("daily_cards", 12))),
            "target_retention": float(
                fields.get("target_retention", current.get("target_retention", 0.90))
            ),
            "focus_subjects_json": focus_json,
            "checkpoints_enabled": int(
                fields.get("checkpoints_enabled", current.get("checkpoints_enabled", 1))
            ),
            "difficulty_pref": str(
                fields.get("difficulty_pref", current.get("difficulty_pref", "balanced"))
            ),
        }
        now = time.time()
        self.db.execute(
            """INSERT INTO owner_settings (
                   owner, timezone, daily_cards, target_retention,
                   focus_subjects_json, checkpoints_enabled, difficulty_pref, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner) DO UPDATE SET
                   timezone = excluded.timezone,
                   daily_cards = excluded.daily_cards,
                   target_retention = excluded.target_retention,
                   focus_subjects_json = excluded.focus_subjects_json,
                   checkpoints_enabled = excluded.checkpoints_enabled,
                   difficulty_pref = excluded.difficulty_pref,
                   updated_at = excluded.updated_at""",
            (
                owner,
                merged["timezone"],
                merged["daily_cards"],
                merged["target_retention"],
                merged["focus_subjects_json"],
                merged["checkpoints_enabled"],
                merged["difficulty_pref"],
                now,
            ),
        )
        return self.get_owner_settings(owner) or {}

    def record_item_report(self, owner: str, item_id: str, kind: str, note: str = "") -> dict[str, Any]:
        report_id = uuid.uuid4().hex
        now = time.time()
        self.db.execute(
            """INSERT INTO item_reports (id, owner, item_id, kind, note, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'open', ?)""",
            (report_id, owner, item_id, kind, note, now),
        )
        count = self.db.query_all(
            "SELECT id FROM item_reports WHERE item_id = ? AND status = 'open'",
            (item_id,),
        )
        if len(count) >= 2:
            self.db.execute(
                "UPDATE items SET verification = 'retired', retired_at = ? WHERE id = ?",
                (now, item_id),
            )
        return self.db.query_one("SELECT * FROM item_reports WHERE id = ?", (report_id,)) or {}

    def reset_practice(self, owner: str) -> dict[str, int]:
        now = time.time()
        deleted: dict[str, int] = {}
        with self.db.transaction() as conn:
            for table in (
                "practice_attempts",
                "student_skill_state",
                "feed_events",
                "feed_cards",
                "feed_plans",
                "checkpoint_answers",
                "checkpoint_runs",
                "owner_settings",
                "item_reports",
            ):
                if table == "checkpoint_answers":
                    conn.execute(
                        """DELETE FROM checkpoint_answers WHERE checkpoint_id IN
                           (SELECT id FROM checkpoint_runs WHERE owner = ?)""",
                        (owner,),
                    )
                    continue
                cursor = conn.execute(f"DELETE FROM {table} WHERE owner = ?", (owner,))
                deleted[table] = int(cursor.rowcount or 0)
            conn.execute(
                "UPDATE items SET verification = 'retired', retired_at = ? WHERE owner = ?",
                (now, owner),
            )
        return deleted

    def evidence_brief(self, owner: str, skill_ids: Iterable[str], attempt_ids: Iterable[str]) -> dict[str, Any]:
        skill_id_list = [str(skill_id) for skill_id in skill_ids if skill_id]
        skills = self.get_skills_by_ids(skill_id_list)
        states = {}
        for skill_id in skill_id_list:
            state = self.get_state(owner, skill_id)
            if state:
                states[skill_id] = state
        chosen_attempts = self.attempts_by_ids(owner, attempt_ids)
        if not chosen_attempts and skill_id_list:
            chosen_attempts = self.recent_skill_attempts(owner, skill_id_list, limit=5)
        attempts = []
        for row in chosen_attempts[:8]:
            attempts.append(
                {
                    "item_id": row.get("item_id"),
                    "skill_ids": _loads(row.get("skill_ids_json"), []),
                    "correct": bool(row.get("correct")),
                    "dont_know": bool(row.get("dont_know")),
                    "selected": _loads(row.get("selected_json"), []),
                    "planner_reason": row.get("planner_reason"),
                }
            )
        return {
            "source": "practice",
            "generated_at": time.time(),
            "skills": [
                {
                    "id": skill.id,
                    "title": skill.title,
                    "subject": skill.subject,
                    "mastery": states[skill.id].mastery if skill.id in states else 0.35,
                    "attempts": states[skill.id].attempts if skill.id in states else 0,
                }
                for skill in skills.values()
            ],
            "attempts": attempts,
        }
