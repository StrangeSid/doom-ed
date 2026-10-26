# SPDX-License-Identifier: GPL-3.0-or-later
"""Plugin registry. Absence of a plugin is a valid, fully supported state."""

from __future__ import annotations

import threading
from typing import Any

from app.integrations.base import LearningContext, LMSPlugin

_PLUGINS: dict[str, LMSPlugin] = {}
_LOCK = threading.Lock()


def register(plugin: LMSPlugin) -> LMSPlugin:
    if not plugin.plugin_id:
        raise ValueError("A plugin needs a stable plugin_id.")
    with _LOCK:
        _PLUGINS[plugin.plugin_id] = plugin
    return plugin


def get(plugin_id: str) -> LMSPlugin | None:
    with _LOCK:
        return _PLUGINS.get(plugin_id)


def all_plugins() -> list[LMSPlugin]:
    with _LOCK:
        return list(_PLUGINS.values())


def available_plugins() -> list[LMSPlugin]:
    return [plugin for plugin in all_plugins() if plugin.is_available()]


def active_plugin(session: Any) -> LMSPlugin | None:
    """Return the first available plugin that is connected for this session."""
    for plugin in available_plugins():
        try:
            if plugin.is_connected(session):
                return plugin
        except Exception:
            continue
    return None


def build_learning_context(
    session: Any,
    db: Any,
    store: Any,
    *,
    refresh: bool = False,
) -> LearningContext:
    """Return optional integration context, or an empty local context."""
    plugin = active_plugin(session)
    if plugin is None:
        return LearningContext()
    return plugin.build_context(session, db, store, refresh=refresh)


def status(session: Any = None) -> list[dict[str, Any]]:
    rows = []
    for plugin in all_plugins():
        available = plugin.is_available()
        connected = False
        if available and session is not None:
            try:
                connected = plugin.is_connected(session)
            except Exception:
                connected = False
        rows.append(
            {
                "id": plugin.plugin_id,
                "name": plugin.display_name,
                "available": available,
                "connected": connected,
                "capabilities": sorted(plugin.capabilities),
            }
        )
    return rows
