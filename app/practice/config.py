# SPDX-License-Identifier: GPL-3.0-or-later
"""Feature flags and tunable defaults for the adaptive practice engine."""

from __future__ import annotations

import os
from dataclasses import dataclass

TRUTHY = {"1", "true", "yes", "on"}


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUTHY


def _int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def practice_engine_enabled() -> bool:
    """The API/engine flag. Off by default until the feed is ready."""
    return _flag("DOOMED_PRACTICE_ENGINE", False)


def home_feed_enabled() -> bool:
    """The UI flag. Kept separate so the API can ship before the home view."""
    return _flag("DOOMED_HOME_FEED", False)


def item_generation_enabled() -> bool:
    return _flag("DOOMED_ITEM_GENERATION", False)


@dataclass(frozen=True)
class PracticeConfig:
    """Defaults for planning and scheduling.

    Every value is a prior, not a claim about a student. Tests pass their own
    config where they need a tighter bound.
    """

    daily_cards: int = 12
    page_limit: int = 6
    max_page_limit: int = 10
    target_success: float = 0.75
    stretch_success: float = 0.55
    repair_success: float = 0.85
    target_mastery: float = 0.75
    target_retention: float = 0.90
    max_new_skills_per_day: int = 2
    max_new_items_per_day: int = 5
    max_attempts_per_skill_per_day: int = 3
    min_verified_items_per_skill: int = 3
    max_checkpoints_per_day: int = 1
    max_attempts_per_minute: int = 60
    max_events_per_minute: int = 300


def get_config() -> PracticeConfig:
    return PracticeConfig(
        daily_cards=_int("DOOMED_DAILY_CARDS", 12, 1, 30),
        page_limit=_int("DOOMED_FEED_PAGE_SIZE", 6, 1, 10),
        max_page_limit=10,
        target_retention=_float("DOOMED_TARGET_RETENTION", 0.90, 0.80, 0.95),
    )
