# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional integration contracts for doom-ed.

Core study features must work with no integration installed. A plugin can add
identity, deadlines, course files, and write actions; it must not become a
required import for the practice engine or the local study feed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class IntegrationNotConnectedError(Exception):
    """The optional integration is installed but this session is not linked."""


class IntegrationUpstreamError(Exception):
    """The optional integration's upstream service failed."""


@dataclass(frozen=True)
class LearningContext:
    """Optional external context for planning and teaching.

    raw_triage keeps the existing /api/study payload shape while the plugin is
    being migrated. The practice engine only reads the fields it needs and can
    run with an empty context.
    """

    source: str = "local"
    connected: bool = False
    raw_triage: dict[str, Any] = field(default_factory=dict)
    auth_error: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)


class LMSPlugin(ABC):
    """A school/LMS integration addon."""

    plugin_id: str = ""
    display_name: str = ""
    capabilities: frozenset[str] = frozenset()

    @abstractmethod
    def is_available(self) -> bool:
        """True when the plugin is installed and enabled by configuration."""

    @abstractmethod
    def is_connected(self, session: Any) -> bool:
        """True when this session has credentials for the upstream service."""

    def authenticate(self, store: Any, email: str, password: str) -> Any:
        raise NotImplementedError

    def fetch_profile(self, session: Any, store: Any) -> dict[str, Any]:
        raise NotImplementedError

    def build_context(self, session: Any, db: Any, store: Any, *, refresh: bool = False) -> LearningContext:
        raise NotImplementedError

    def search_files(self, session: Any, db: Any, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        raise NotImplementedError

    def browse_folder(self, session: Any, db: Any, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    def confirm_write(self, write_mgr: Any, session: Any, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def execute_write(self, write_mgr: Any, session: Any, token_id: str) -> dict[str, Any]:
        raise NotImplementedError
