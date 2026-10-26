# SPDX-License-Identifier: GPL-3.0-or-later
"""Answer checking, BKT/Elo mastery updates, and FSRS-lite scheduling.

This module is pure: no database, no network, no model calls. The service layer
persists the result. That keeps the learning model testable and auditable.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from app.practice.models import DONT_KNOW, GradeOutcome, Item, StudentState

_DAY = 86400.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _as_list(selected: Any) -> list[Any]:
    if selected is None:
        return []
    if isinstance(selected, (list, tuple, set)):
        return list(selected)
    return [selected]


def is_dont_know(selected: Any, *, numeric: bool = False) -> bool:
    values = _as_list(selected)
    if len(values) != 1:
        return False
    value = values[0]
    if value == DONT_KNOW:
        return True
    if numeric:
        return False
    return value == 0 or value == "0"


def _numeric(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("A boolean is not a numeric answer.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("A numeric answer was expected.") from exc
    if not math.isfinite(number):
        raise ValueError("A finite numeric answer was expected.")
    return number


def grade_answer(item: Item, selected: Any) -> GradeOutcome:
    """Grade a selection against the item's server-only solution.

    Raises ValueError for malformed selections. The caller maps that to 422 and
    must not record an attempt or update mastery.
    """
    solution = item.solution or {}
    explanation = str(solution.get("explanation") or "")
    values = _as_list(selected)

    if item.format in ("mcq_single", "mcq_multi"):
        if is_dont_know(values):
            return GradeOutcome(False, True, (), explanation, tuple(values))
        valid_indices = {int(option.get("index", i + 1)) for i, option in enumerate(item.options)}
        try:
            chosen = {int(value) for value in values}
        except (TypeError, ValueError) as exc:
            raise ValueError("Option indices must be integers.") from exc
        if not chosen or not chosen <= valid_indices:
            raise ValueError("Selected option is outside this item.")
        correct = {int(value) for value in solution.get("correct") or []}
        if not correct:
            raise ValueError("Item has no correct option.")
        return GradeOutcome(chosen == correct, False, tuple(sorted(correct)), explanation, tuple(values))

    if item.format == "numeric":
        if is_dont_know(values, numeric=True):
            return GradeOutcome(False, True, (), explanation, tuple(values))
        if len(values) != 1:
            raise ValueError("Exactly one numeric answer is required.")
        answer = _numeric(values[0])
        accepted = solution.get("value", solution.get("values"))
        accepted_values = accepted if isinstance(accepted, list) else [accepted]
        targets = [_numeric(value) for value in accepted_values if value is not None]
        if not targets:
            raise ValueError("Item has no numeric answer.")
        tolerance = solution.get("tolerance")
        for target in targets:
            tol = _numeric(tolerance) if tolerance is not None else max(1e-6, abs(target) * 1e-3)
            if abs(answer - target) <= tol:
                return GradeOutcome(True, False, (), explanation, (answer,))
        return GradeOutcome(False, False, (), explanation, (answer,))

    raise ValueError(f"Unsupported item format: {item.format}")


def bkt_update(
    mastery: float,
    correct: bool,
    dont_know: bool,
    *,
    guess: float = 0.25,
    slip: float = 0.10,
    transition: float = 0.15,
    unknown_slip: float = 0.35,
) -> float:
    """Bayesian Knowledge Tracing posterior after one observation."""
    p = _clamp(float(mastery), 0.01, 0.99)
    prior = p + (1.0 - p) * transition
    if dont_know:
        s = unknown_slip
        post = (prior * s) / (prior * s + (1.0 - prior) * (1.0 - guess))
    elif correct:
        post = (prior * (1.0 - slip)) / (prior * (1.0 - slip) + (1.0 - prior) * guess)
    else:
        post = (prior * slip) / (prior * slip + (1.0 - prior) * (1.0 - guess))
    return _clamp(post, 0.01, 0.99)


def elo_update(
    ability: float,
    difficulty: float,
    correct: bool,
    *,
    attempts: int,
    item_attempts: int,
    confidence: int | None = None,
) -> tuple[float, float]:
    """Update learner ability and item difficulty on a common logistic scale."""
    expected = sigmoid(ability - difficulty)
    y = 1.0 if correct else 0.0
    confidence_multiplier = {1: 0.6, 2: 1.0, 3: 1.25}.get(confidence or 2, 1.0)
    learner_k = max(0.06, 0.30 / (1.0 + max(0, attempts) / 8.0))
    item_k = max(0.01, 0.08 / (1.0 + max(0, item_attempts) / 20.0))
    delta = y - expected
    new_ability = _clamp(ability + learner_k * delta * confidence_multiplier, -4.0, 4.0)
    new_difficulty = _clamp(difficulty - item_k * delta, -4.0, 4.0)
    return new_ability, new_difficulty


def scheduler_update(
    stability: float,
    mastery: float,
    correct: bool,
    dont_know: bool,
    *,
    now: float,
    target_retention: float = 0.90,
) -> tuple[float, float]:
    """Return (new stability days, next due timestamp).

    The curve is the FSRS shape with R(t,S) = (1 + t/(9S))^-1. A correct
    retrieval grows stability; a miss or an unknown queues a short repair.
    """
    s = max(0.5, min(365.0, float(stability)))
    if correct:
        s_next = min(365.0, max(0.5, s * (1.0 + 0.7 * (1.0 - _clamp(mastery, 0.0, 1.0)))))
        interval = 9.0 * s_next * (1.0 / _clamp(target_retention, 0.80, 0.95) - 1.0)
        interval = _clamp(interval, 1.0, 180.0)
        return s_next, now + interval * _DAY
    if dont_know:
        return max(0.5, s * 0.5), now + 600.0
    return max(0.5, s * 0.3), now + 600.0


def mastery_band(mastery: float) -> str:
    if mastery >= 0.75:
        return "solid"
    if mastery >= 0.40:
        return "learning"
    return "weak"


def update_state(
    state: StudentState,
    *,
    item: Item,
    correct: bool,
    dont_know: bool,
    item_attempts: int,
    now: float,
    confidence: int | None = None,
    target_retention: float = 0.90,
) -> tuple[StudentState, float]:
    """Apply one graded attempt to a skill state. Returns (state, new item difficulty)."""
    guess = item.guessing or 1.0 / max(1, len(item.options) or 4)
    mastery = bkt_update(state.mastery, correct, dont_know, guess=guess)
    ability, difficulty = elo_update(
        state.ability,
        item.difficulty,
        correct,
        attempts=state.attempts,
        item_attempts=item_attempts,
        confidence=confidence,
    )
    stability, due_at = scheduler_update(
        state.stability,
        mastery,
        correct,
        dont_know,
        now=now,
        target_retention=target_retention,
    )
    attempts = state.attempts + 1
    correct_count = state.correct + (1 if correct else 0)
    unknowns = state.unknowns + (1 if dont_know else 0)
    streak = state.streak + 1 if correct else 0
    eligible = bool(attempts >= 4 and correct_count >= 3 and mastery >= 0.60)
    return (
        replace(
            state,
            ability=ability,
            mastery=mastery,
            attempts=attempts,
            correct=correct_count,
            unknowns=unknowns,
            streak=streak,
            stability=stability,
            due_at=due_at,
            last_seen=now,
            checkpoint_eligible=eligible,
        ),
        difficulty,
    )
