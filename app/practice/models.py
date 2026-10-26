# SPDX-License-Identifier: GPL-3.0-or-later
"""Small typed models shared by the practice store, planner, and service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DONT_KNOW = "__dont_know__"


@dataclass(frozen=True)
class Skill:
    id: str
    subject: str
    title: str
    level: str = ""
    unit: str = ""
    description: str = ""
    ib_code: str = ""
    prereq_ids: tuple[str, ...] = ()
    exam_weight: float = 1.0
    source_kind: str = ""
    source_ref: str = ""


@dataclass(frozen=True)
class Item:
    id: str
    skill_ids: tuple[str, ...]
    format: str
    stem: str
    options: tuple[dict[str, Any], ...]
    solution: dict[str, Any] = field(default_factory=dict, repr=False)
    difficulty: float = 0.0
    discrimination: float = 1.0
    guessing: float = 0.25
    owner: str | None = None
    version: int = 1
    source_kind: str = "generated"
    source_ref: str = ""
    source_sha256: str = ""
    verification: str = "draft"
    content_hash: str = ""
    retired_at: float | None = None

    @property
    def primary_skill(self) -> str:
        return self.skill_ids[0] if self.skill_ids else ""


@dataclass
class StudentState:
    owner: str
    skill_id: str
    ability: float = 0.0
    mastery: float = 0.35
    attempts: int = 0
    correct: int = 0
    unknowns: int = 0
    streak: int = 0
    stability: float = 1.0
    due_at: float | None = None
    last_seen: float | None = None
    checkpoint_eligible: bool = False


@dataclass(frozen=True)
class PlannedCard:
    kind: str
    item_id: str | None
    skill_ids: tuple[str, ...]
    reason_code: str
    reason_text: str
    score: float = 0.0


@dataclass(frozen=True)
class GradeOutcome:
    correct: bool
    dont_know: bool
    correct_options: tuple[int, ...]
    explanation: str
    selected: tuple[Any, ...] = ()
