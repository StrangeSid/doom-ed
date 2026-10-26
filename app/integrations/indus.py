# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in Indus LMS plugin adapter.

The legacy bridge is kept behind this adapter so core modules do not import it
directly. Removing or disabling the plugin leaves local study fully functional.
"""

from __future__ import annotations

import os
from typing import Any

from app.integrations.base import LearningContext, LMSPlugin


def _enabled() -> bool:
    return os.environ.get("DOOMED_LMS_PLUGIN", "1").strip().lower() in {"1", "true", "yes", "on"}


class IndusLMSPlugin(LMSPlugin):
    plugin_id = "indus"
    display_name = "Indus LMS"
    capabilities = frozenset({"auth", "context", "files", "writes"})

    def is_available(self) -> bool:
        if not _enabled():
            return False
        try:
            import app.lms_bridge as bridge
        except Exception:
            return False
        # The adapter can exist while the upstream induslms-agent package is
        # missing. In that case the plugin is installed but not available.
        return getattr(bridge, "lms", None) is not None

    def is_connected(self, session: Any) -> bool:
        return bool(getattr(session, "is_lms_authenticated", False))

    def authenticate(self, store: Any, email: str, password: str) -> Any:
        from app.lms_bridge import login_indus

        return login_indus(store, email, password)

    def fetch_profile(self, session: Any, store: Any) -> dict[str, Any]:
        from app.lms_bridge import fetch_student_payload

        return fetch_student_payload(session, store, refresh=False)

    def build_context(self, session: Any, db: Any, store: Any, *, refresh: bool = False) -> LearningContext:
        from app.lms_bridge import build_study_triage

        triage = build_study_triage(session, db, store, refresh=refresh)
        auth_error = triage.pop("_auth_error", None)
        return LearningContext(
            source=self.plugin_id,
            connected=True,
            raw_triage=triage,
            auth_error=auth_error,
            capabilities=self.capabilities,
        )

    def search_files(self, session: Any, db: Any, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        from app.lms_bridge import search_files

        return search_files(session, db, query, **kwargs)

    def browse_folder(self, session: Any, db: Any, **kwargs: Any) -> dict[str, Any]:
        from app.lms_bridge import browse_lms_folder

        return browse_lms_folder(session, db, **kwargs)

    def confirm_write(self, write_mgr: Any, session: Any, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        from app.lms_bridge import request_write_confirmation

        return request_write_confirmation(write_mgr, session, action, payload)

    def execute_write(self, write_mgr: Any, session: Any, token_id: str) -> dict[str, Any]:
        from app.lms_bridge import execute_confirmed_write

        return execute_confirmed_write(write_mgr, session, token_id)
