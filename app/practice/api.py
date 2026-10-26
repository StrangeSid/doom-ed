# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTP API for the adaptive practice feed.

The router is mounted by app.main only when configured, and every endpoint
checks DOOMED_PRACTICE_ENGINE at request time. The default is off so existing
deployments and tests keep their old behavior until the flag is set.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Cookie, HTTPException, Request
from pydantic import BaseModel, Field

from app.practice import config as practice_config
from app.practice.service import ConflictError, PracticeService

router = APIRouter(prefix="/api")

_service: PracticeService | None = None
_session_resolver: Callable[[Request, str | None], Any] | None = None


def configure(service: PracticeService, session_resolver: Callable[[Request, str | None], Any]) -> None:
    global _service, _session_resolver
    _service = service
    _session_resolver = session_resolver


def _get_service() -> PracticeService:
    if _service is None:
        raise HTTPException(status_code=503, detail="Practice engine is not configured.")
    return _service


def _require_enabled() -> None:
    if not practice_config.practice_engine_enabled():
        raise HTTPException(status_code=404, detail="Practice engine is disabled.")


def _session(request: Request, doomed_sid: str | None) -> Any:
    if _session_resolver is None:
        raise HTTPException(status_code=503, detail="Session resolver is not configured.")
    return _session_resolver(request, doomed_sid)


class AttemptRequest(BaseModel):
    card_id: str
    item_id: str
    item_version: int = 1
    selected: Any = None
    attempt_token: str
    confidence: int | None = None
    latency_ms: int | None = None


class TeachRequest(BaseModel):
    skill_ids: list[str] = Field(default_factory=list)
    attempt_ids: list[str] = Field(default_factory=list)
    intent: str = "teach"


class EventItem(BaseModel):
    type: str
    card_id: str | None = None
    plan_id: str | None = None
    value: Any = None


class EventsRequest(BaseModel):
    events: list[EventItem] = Field(default_factory=list)


class ReportRequest(BaseModel):
    item_id: str
    kind: str
    note: str = ""


class SettingsRequest(BaseModel):
    timezone: str | None = None
    daily_cards: int | None = None
    target_retention: float | None = None
    checkpoints_enabled: bool | None = None
    difficulty_pref: str | None = None


class ResetRequest(BaseModel):
    confirm: bool = False


@router.get("/home/feed")
def home_feed(
    request: Request,
    day: str | None = None,
    cursor: int = 0,
    limit: int | None = None,
    timezone: str | None = None,
    refresh: bool = False,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    _require_enabled()
    sess = _session(request, doomed_sid)
    result = _get_service().plan_feed(
        sess.owner,
        subjects=getattr(sess, "subjects", []) or [],
        cursor=cursor,
        limit=limit,
        day=day,
        timezone=timezone,
        target_session=str(getattr(sess, "target_session", "") or ""),
        refresh=refresh,
    )
    return {"status": "ok", **result}


@router.post("/practice/attempts")
def submit_attempt(
    body: AttemptRequest,
    request: Request,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    _require_enabled()
    sess = _session(request, doomed_sid)
    confidence = body.confidence if body.confidence in (1, 2, 3) else None
    try:
        result = _get_service().grade_attempt(
            sess.owner,
            card_id=body.card_id,
            item_id=body.item_id,
            item_version=body.item_version,
            selected=body.selected,
            attempt_token=body.attempt_token,
            confidence=confidence,
            latency_ms=body.latency_ms,
            source="feed",
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return result


@router.post("/home/events")
def submit_events(
    body: EventsRequest,
    request: Request,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    _require_enabled()
    sess = _session(request, doomed_sid)
    events = [event.model_dump() for event in body.events]
    count = _get_service().add_events(sess.owner, events)
    return {"status": "ok", "accepted": count}


@router.get("/practice/progress")
def practice_progress(
    request: Request,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    _require_enabled()
    sess = _session(request, doomed_sid)
    return {"status": "ok", **_get_service().progress(sess.owner)}


@router.post("/practice/teach")
def practice_teach(
    body: TeachRequest,
    request: Request,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    _require_enabled()
    sess = _session(request, doomed_sid)
    try:
        result = _get_service().teach(
            sess.owner,
            skill_ids=body.skill_ids,
            attempt_ids=body.attempt_ids,
            intent=body.intent,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return result


@router.post("/practice/feedback")
def practice_feedback(
    body: ReportRequest,
    request: Request,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    _require_enabled()
    sess = _session(request, doomed_sid)
    try:
        return _get_service().report_item(sess.owner, body.item_id, body.kind, body.note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/practice/settings")
def practice_settings(
    body: SettingsRequest,
    request: Request,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    _require_enabled()
    sess = _session(request, doomed_sid)
    fields = body.model_dump(exclude_none=True)
    return {"status": "ok", "settings": _get_service().update_settings(sess.owner, **fields)}


@router.post("/practice/reset")
def practice_reset(
    body: ResetRequest,
    request: Request,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    _require_enabled()
    sess = _session(request, doomed_sid)
    try:
        return _get_service().reset(sess.owner, confirm=body.confirm)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
