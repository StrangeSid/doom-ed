# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure adaptive selection for the home feed.

No database, no network, no model calls. Given a snapshot of skills, items,
student state, and a seed, the same plan is produced every time. The service
layer materializes the result into feed_cards.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Iterable

from app.practice.config import PracticeConfig
from app.practice.grading import mastery_band, sigmoid
from app.practice.models import Item, PlannedCard, Skill, StudentState

_DAY = 86400.0
_PRIOR_MASTERY = 0.35


@dataclass(frozen=True)
class _Candidate:
    item: Item
    score: float
    reason_code: str
    reason_text: str
    expected_success: float
    is_new_skill: bool


def _state(states: dict[str, StudentState], skill_id: str) -> StudentState | None:
    return states.get(skill_id)


def _average_mastery(states: dict[str, StudentState], skill_ids: Iterable[str]) -> float:
    values = []
    for skill_id in skill_ids:
        state = _state(states, skill_id)
        values.append(state.mastery if state else _PRIOR_MASTERY)
    return sum(values) / max(1, len(values))


def _due_pressure(states: dict[str, StudentState], skill_ids: Iterable[str], now: float) -> float:
    pressure = 0.0
    for skill_id in skill_ids:
        state = _state(states, skill_id)
        if not state or state.due_at is None:
            continue
        if state.due_at <= now:
            pressure = max(pressure, 1.0)
        else:
            # A due time in the next two days still deserves some weight.
            pressure = max(pressure, max(0.0, 1.0 - (state.due_at - now) / (2.0 * _DAY)))
    return pressure


def _novelty(states: dict[str, StudentState], skill_ids: Iterable[str], now: float) -> float:
    best = 0.0
    for skill_id in skill_ids:
        state = _state(states, skill_id)
        if state is None or state.attempts == 0:
            best = max(best, 1.0)
        elif state.last_seen is not None and now - state.last_seen > 14 * _DAY:
            best = max(best, 0.6)
    return best


def _prerequisites_met(
    item: Item,
    skills: dict[str, Skill],
    states: dict[str, StudentState],
    *,
    floor: float = 0.50,
) -> bool:
    # A skill cannot be its own prerequisite, and a repair item may target a
    # prerequisite that is still weak. For normal items, all prerequisites
    # need evidence above the floor.
    item_skill_ids = set(item.skill_ids)
    for skill_id in item.skill_ids:
        skill = skills.get(skill_id)
        if skill is None:
            return False
        for prereq_id in skill.prereq_ids:
            if prereq_id in item_skill_ids:
                continue
            state = states.get(prereq_id)
            if state is None or state.mastery < floor:
                return False
    return True


def _reason_text(code: str, skill: Skill, states: dict[str, StudentState], now: float) -> str:
    state = states.get(skill.id)
    if code == "due_review":
        if state and state.last_seen:
            days = max(1, int((now - state.last_seen) / _DAY))
            return f"Due for review — last seen {days} day{'s' if days != 1 else ''} ago."
        return "Due for review."
    if code == "weak_skill":
        mastery = state.mastery if state else _PRIOR_MASTERY
        return f"You are at {mastery_band(mastery)} on {skill.title}."
    if code == "exam_focus":
        return f"High-yield practice for {skill.subject}."
    if code == "new_skill":
        return f"New topic: {skill.title}."
    return f"Builds on {skill.title}."


def _candidate_for(
    item: Item,
    skills: dict[str, Skill],
    states: dict[str, StudentState],
    *,
    now: float,
    recent_skill_ids: list[str],
    config: PracticeConfig,
) -> _Candidate | None:
    if not item.skill_ids or any(skill_id not in skills for skill_id in item.skill_ids):
        return None
    primary = skills[item.skill_ids[0]]
    mastery = _average_mastery(states, item.skill_ids)
    due = _due_pressure(states, item.skill_ids, now)
    gap = max(0.0, config.target_mastery - mastery)
    exam = max(0.0, min(1.0, max(skills[sid].exam_weight for sid in item.skill_ids)))
    novelty = _novelty(states, item.skill_ids, now)
    recent = 1.0 if any(sid in recent_skill_ids[-3:] for sid in item.skill_ids) else 0.0
    state = states.get(primary.id)
    ability = state.ability if state else 0.0
    expected = sigmoid(ability - item.difficulty)
    fit_penalty = abs(expected - config.target_success)
    score = (
        1.00 * due
        + 0.90 * gap
        + 0.60 * exam
        + 0.20 * novelty
        - 0.50 * recent
        - 0.80 * fit_penalty
    )
    is_new = not any(
        (states.get(skill_id) is not None and states[skill_id].attempts > 0)
        for skill_id in item.skill_ids
    )
    # Reason codes follow what the student would find useful, not just the
    # largest weighted term. A brand-new skill is best described as new; a
    # due review is best described as due.
    if is_new:
        code = "new_skill"
    elif due >= 0.50:
        code = "due_review"
    elif gap >= 0.25:
        code = "weak_skill"
    elif exam >= 0.50:
        code = "exam_focus"
    elif novelty >= 0.50:
        code = "new_skill"
    else:
        code = "practice"
    return _Candidate(
        item=item,
        score=score,
        reason_code=code,
        reason_text=_reason_text(code, primary, states, now),
        expected_success=expected,
        is_new_skill=is_new,
    )


def select_cards(
    *,
    owner: str,
    states: dict[str, StudentState],
    skills: dict[str, Skill],
    items: list[Item],
    enrolled_subjects: set[str],
    recent_item_ids: set[str] | None = None,
    recent_skill_ids: list[str] | None = None,
    attempts_by_skill_today: dict[str, int] | None = None,
    skipped_skill_ids: set[str] | None = None,
    now: float,
    seed: int,
    goal: int,
    config: PracticeConfig | None = None,
) -> list[PlannedCard]:
    """Choose up to the requested number of cards from verified items.

    Hard constraints are applied before scoring: owner visibility, verified
    status, subject scope, prerequisites, daily caps, and skipped skills.
    """
    cfg = config or PracticeConfig()
    recent_items = recent_item_ids or set()
    recent_skills = list(recent_skill_ids or [])
    attempts_today = attempts_by_skill_today or {}
    skipped = skipped_skill_ids or set()
    target_goal = max(0, min(int(goal), 30))

    candidates: list[_Candidate] = []
    for item in items:
        if item.verification != "verified" or item.retired_at is not None:
            continue
        if item.owner not in (None, owner):
            continue
        if not item.skill_ids:
            continue
        if enrolled_subjects and not any(
            skills.get(skill_id) is not None and skills[skill_id].subject in enrolled_subjects
            for skill_id in item.skill_ids
        ):
            continue
        if any(skill_id in skipped for skill_id in item.skill_ids):
            continue
        if any(
            attempts_today.get(skill_id, 0) >= cfg.max_attempts_per_skill_per_day
            for skill_id in item.skill_ids
        ):
            continue
        if not _prerequisites_met(item, skills, states):
            continue
        if item.id in recent_items and _due_pressure(states, item.skill_ids, now) < 0.5:
            continue
        candidate = _candidate_for(
            item,
            skills,
            states,
            now=now,
            recent_skill_ids=recent_skills,
            config=cfg,
        )
        if candidate is not None:
            candidates.append(candidate)

    if not candidates:
        return []

    rng = random.Random(seed)
    selected: list[PlannedCard] = []
    used_item_ids: set[str] = set()
    introduced_skill_ids: set[str] = set()
    recent_skills = list(recent_skills)
    new_skills = 0
    new_items = 0

    while len(selected) < target_goal:
        pool: list[_Candidate] = []
        for candidate in candidates:
            item = candidate.item
            if item.id in used_item_ids:
                continue
            introduces_new_skill = candidate.is_new_skill and not (
                set(item.skill_ids) & introduced_skill_ids
            )
            if introduces_new_skill and new_skills >= cfg.max_new_skills_per_day:
                continue
            if candidate.is_new_skill and new_items >= cfg.max_new_items_per_day:
                continue
            pool.append(candidate)
        if not pool:
            break

        # Prefer variety: if there are enough alternatives, avoid repeating the
        # last two skills back-to-back.
        recent_two = set(recent_skills[-2:])
        varied = [c for c in pool if not (set(c.item.skill_ids) & recent_two)]
        if len(varied) >= 2:
            pool = varied

        pool.sort(key=lambda candidate: (-candidate.score, candidate.item.id))
        top = pool[:10]
        if len(top) == 1:
            chosen = top[0]
        else:
            temperature = 0.35
            weights = [math.exp(candidate.score / temperature) for candidate in top]
            chosen = rng.choices(top, weights=weights, k=1)[0]

        used_item_ids.add(chosen.item.id)
        selected.append(
            PlannedCard(
                kind="practice",
                item_id=chosen.item.id,
                skill_ids=chosen.item.skill_ids,
                reason_code=chosen.reason_code,
                reason_text=chosen.reason_text,
                score=round(chosen.score, 6),
            )
        )
        recent_skills.extend(chosen.item.skill_ids)
        if chosen.is_new_skill:
            new_items += 1
        new_skill_ids = set(chosen.item.skill_ids) - introduced_skill_ids
        new_skills += len(new_skill_ids)
        introduced_skill_ids.update(chosen.item.skill_ids)

    return selected
