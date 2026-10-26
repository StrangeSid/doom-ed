# SPDX-License-Identifier: GPL-3.0-or-later
"""Adaptive practice engine for the home study feed.

The practice package is intentionally independent of any LMS integration. It
consumes owner-scoped skills, items, attempts, and an optional learning context;
LMS data arrives through app.integrations when a plugin is connected.
"""

from app.practice.config import get_config, practice_engine_enabled
from app.practice.models import Item, PlannedCard, Skill, StudentState
from app.practice.planner import select_cards
from app.practice.service import PracticeService
from app.practice.store import PracticeStore

__all__ = [
    "Item",
    "PlannedCard",
    "PracticeService",
    "PracticeStore",
    "Skill",
    "StudentState",
    "get_config",
    "practice_engine_enabled",
    "select_cards",
]
