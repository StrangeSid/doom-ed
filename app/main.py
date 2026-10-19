# SPDX-License-Identifier: GPL-3.0-or-later
"""FastAPI application for doom-ed: AI Study Coach & Pedagogy System.

Provides:
- Cookie-authenticated Indus LMS sessions with Fernet encryption at rest.
- Grounded study coach triage (/api/study) from live LMS data (zero demo fallbacks).
- SSE streaming Socratic chat (/api/chat/send) with interactive turn-gated cards.
- Bidirectional chat ↔ slide-over file viewer (/api/files/{sha256}) with ACL gates.
- IB resource search proxy (/api/ib/search) with automatic paper+markscheme pairing.
- Static mount for vanilla zero-build web interface (web/).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import (
    Cookie,
    FastAPI,
    File,
    Form,
    HTTPException,
    Header,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse  # type: ignore

from app import cache, config, file_reader, ib_client, lms_bridge
from app.db import Database
from app.orchestrator import Orchestrator
from app.security import Cipher, WriteConfirmationManager
from app.sessions import Session, SessionStore, import_local_indus_token

COOKIE_NAME = "doomed_sid"
ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"
DATA_DIR = config.ensure_data_dir()

app = FastAPI(
    title="doom-ed",
    description="AI Study Coach & Pedagogy System grounded in Indus LMS and IB Resources",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize singletons
session_store = SessionStore(DATA_DIR / "sessions.sqlite")
cache.set_store(session_store)
db = Database(DATA_DIR / "doomed.sqlite")
write_mgr = WriteConfirmationManager()
orchestrator = Orchestrator(db=db, write_mgr=write_mgr)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "app": "doom-ed", "version": "0.1.0"}


# Auto-import local indus token on startup if available
@app.on_event("startup")
def startup_init() -> None:
    token_path = Path(os.path.expanduser(config.INDUSLMS_TOKEN_FILE))
    if token_path.exists():
        try:
            import_local_indus_token(session_store, token_path=token_path)
        except Exception:
            pass


def get_current_session(request: Request, doomed_sid: str | None = Cookie(None)) -> Session:
    """Retrieve authenticated session from HttpOnly cookie or bootstrap local token."""
    sid = doomed_sid or request.headers.get("x-session-id")
    sess = session_store.get(sid) if sid else None
    if not sess and os.environ.get("DOOMED_AUTO_BOOTSTRAP_TOKEN", "1") == "1":
        sess = import_local_indus_token(session_store)
    if not sess:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please sign in to Indus LMS.",
        )
    return sess


# --- Auth Routes -------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/api/login")
def login(body: LoginRequest, response: Response) -> dict[str, Any]:
    """Authenticate with Indus LMS credentials and set HttpOnly session cookie."""
    try:
        sess = lms_bridge.login_indus(session_store, body.email, body.password)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))

    response.set_cookie(
        key=COOKIE_NAME,
        value=sess.sid,
        httponly=True,
        samesite="lax",
        max_age=86400 * 30,  # 30 days
        secure=False,
    )
    return {
        "status": "ok",
        "student": {
            "name": sess.name,
            "email": sess.email,
            "uid": sess.uid,
            "tenant": sess.tenant,
            "grade": sess.grade,
        },
    }


@app.post("/api/logout")
def logout(response: Response, doomed_sid: str | None = Cookie(None)) -> dict[str, str]:
    if doomed_sid:
        session_store.drop(doomed_sid)
        cache.drop(doomed_sid)
    response.delete_cookie(COOKIE_NAME)
    return {"status": "ok"}


@app.get("/api/session")
def session_info(
    request: Request,
    response: Response,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    sess = get_current_session(request, doomed_sid)
    if not doomed_sid and sess:
        response.set_cookie(
            key=COOKIE_NAME,
            value=sess.sid,
            httponly=True,
            samesite="lax",
            max_age=86400 * 30,
            secure=False,
        )
    student_info = {
        "name": sess.name,
        "email": sess.email,
        "uid": sess.uid,
        "tenant": sess.tenant,
        "grade": sess.grade,
    }
    try:
        payload = lms_bridge.fetch_student_payload(sess, session_store, refresh=False)
        if isinstance(payload.get("student"), dict):
            student_info.update(payload["student"])
    except Exception:
        pass

    return {
        "status": "ok",
        "student": student_info,
    }


# --- Study Coach Triage ------------------------------------------------------


@app.get("/api/study")
def study_triage(
    request: Request,
    refresh: bool = False,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    """Return live student study triage (now, weak_topics, before_break). Zero demo data."""
    sess = get_current_session(request, doomed_sid)
    try:
        triage = lms_bridge.build_study_triage(sess, db, session_store, refresh=refresh)
        return {"status": "ok", "data": triage}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))


# --- Threads & Chat ----------------------------------------------------------


class CreateThreadRequest(BaseModel):
    title: str
    topic: str | None = None
    subject: str | None = None


@app.get("/api/threads")
def list_threads(request: Request, doomed_sid: str | None = Cookie(None)) -> dict[str, Any]:
    sess = get_current_session(request, doomed_sid)
    threads = db.list_threads(sess.owner)
    return {"status": "ok", "threads": threads}


@app.post("/api/threads")
def create_thread(
    body: CreateThreadRequest,
    request: Request,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    sess = get_current_session(request, doomed_sid)
    thread = db.create_thread(
        owner=sess.owner,
        title=body.title,
        topic=body.topic,
        subject=body.subject,
    )
    return {"status": "ok", "thread": thread}


@app.get("/api/threads/{thread_id}")
def get_thread(
    thread_id: str,
    request: Request,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    sess = get_current_session(request, doomed_sid)
    thread = db.get_thread(sess.owner, thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found.")
    messages = db.list_messages(thread_id)
    attachments = db.list_thread_attachments(thread_id)
    return {
        "status": "ok",
        "thread": thread,
        "messages": messages,
        "attachments": attachments,
    }


@app.delete("/api/threads/{thread_id}")
def delete_thread(
    thread_id: str,
    request: Request,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    sess = get_current_session(request, doomed_sid)
    deleted = db.delete_thread(sess.owner, thread_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Thread not found.")
    return {"status": "ok"}


class ChatSendRequest(BaseModel):
    thread_id: str | None = None
    message: str = ""
    attached_refs: list[str] | None = None


@app.post("/api/chat/send")
async def chat_send(
    body: ChatSendRequest,
    request: Request,
    doomed_sid: str | None = Cookie(None),
) -> EventSourceResponse:
    """Stream multi-turn Socratic responses and tool events via SSE."""
    sess = get_current_session(request, doomed_sid)

    thread_id = (body.thread_id or "").strip()
    created_thread: dict[str, Any] | None = None
    if not thread_id:
        title = " ".join((body.message or "").split()[:8]).strip()
        if len(title) > 80:
            title = title[:80].rstrip()
        created_thread = db.create_thread(owner=sess.owner, title=title or "New Lesson")
        thread_id = str(created_thread.get("id") or "")

    # Fetch fresh student triage for teacher grounding
    triage = None
    try:
        triage = lms_bridge.build_study_triage(sess, db, session_store, refresh=False)
    except Exception:
        pass

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        try:
            if created_thread:
                yield {
                    "event": "thread_created",
                    "data": json.dumps({"thread_id": thread_id, "thread": created_thread}),
                }
            events = orchestrator.process_user_turn(
                session=sess,
                thread_id=thread_id,
                user_message=body.message,
                student_triage=triage,
                attached_refs=body.attached_refs,
            )
            for ev in events:
                yield {
                    "event": ev["event"],
                    "data": json.dumps(ev["data"]),
                }
        except Exception as e:
            yield {
                "event": "error",
                "data": json.dumps({"error": str(e)}),
            }

    return EventSourceResponse(event_generator())


class CardReplyRequest(BaseModel):
    thread_id: str
    card_id: str
    reply: dict[str, Any]


@app.post("/api/chat/card-reply")
def card_reply(
    body: CardReplyRequest,
    request: Request,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    """Submit response to QuizCard, AskCard, or WriteConfirmCard."""
    sess = get_current_session(request, doomed_sid)
    try:
        res = orchestrator.submit_card_reply(
            session=sess,
            thread_id=body.thread_id,
            card_id=body.card_id,
            reply_data=body.reply,
        )
        return {"status": "ok", "result": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class VizFeedbackRequest(BaseModel):
    call_id: str
    feedback: dict[str, Any]


@app.post("/api/chat/viz-feedback")
def viz_feedback(
    body: VizFeedbackRequest,
    request: Request,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    """Browser returns DOM validation & canvas PNG for visual maker inspection loop."""
    sess = get_current_session(request, doomed_sid)
    res = orchestrator.handle_viz_feedback(sess, body.call_id, body.feedback)
    return res


@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    request: Request = None,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    """Upload a file, encrypt it at rest, and grant owner access."""
    sess = get_current_session(request, doomed_sid)
    raw = await file.read()
    if len(raw) > config.MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail="File exceeds maximum allowed size (25MB).")

    sha256 = hashlib.sha256(raw).hexdigest().lower()
    ref = f"upload://{sha256}"
    res = file_reader.ingest_bytes(
        db=db,
        owner=sess.owner,
        raw_bytes=raw,
        filename=file.filename or "upload.bin",
        source_type="upload",
        ref=ref,
        mime=file.content_type or "",
    )
    return {"status": "ok", "sha256": res["sha256"], "ref": res["ref"], "filename": res["filename"]}


# --- File Viewer & Blobs -----------------------------------------------------


@app.get("/api/files/{sha256}")
def serve_file(
    sha256: str,
    request: Request,
    range: str | None = Header(None),
    doomed_sid: str | None = Cookie(None),
) -> Response:
    """Serve decrypted file to in-app slide-over viewer with strict ACL and Range support."""
    sess = get_current_session(request, doomed_sid)
    try:
        file_row, raw_bytes = file_reader.get_file_bytes(db, sess.owner, sha256)
    except PermissionError as pe:
        raise HTTPException(status_code=403, detail=str(pe))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found.")

    total_len = len(raw_bytes)
    mime = file_row.get("mime") or "application/octet-stream"
    fname = file_row.get("filename") or f"{sha256}.bin"

    # Support HTTP Range requests for PDF.js page streaming
    if range and range.startswith("bytes="):
        range_spec = range[6:].strip()
        parts = range_spec.split("-", 1)
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else total_len - 1
        start = max(0, min(start, total_len - 1))
        end = max(start, min(end, total_len - 1))
        chunk_len = end - start + 1
        headers = {
            "Content-Range": f"bytes {start}-{end}/{total_len}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(chunk_len),
            "Content-Type": mime,
            "Cache-Control": "private, no-cache",
        }
        return Response(content=raw_bytes[start : end + 1], status_code=206, headers=headers)

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(total_len),
        "Content-Type": mime,
        "Content-Disposition": f'inline; filename="{fname}"',
        "Cache-Control": "private, no-cache",
    }
    return Response(content=raw_bytes, status_code=200, headers=headers)


@app.get("/api/viz/{visual_id}/png")
def serve_visual_png(visual_id: str, request: Request, doomed_sid: str | None = Cookie(None)) -> Response:
    sess = get_current_session(request, doomed_sid)
    viz = db.get_visual(visual_id)
    if not viz or viz.get("owner") != sess.owner:
        raise HTTPException(status_code=404, detail="Visual not found.")
    png_path = viz.get("png_path")
    if not png_path or not Path(png_path).exists():
        raise HTTPException(status_code=404, detail="PNG render missing.")
    return FileResponse(png_path, media_type="image/png")


# --- IB Search Proxy ---------------------------------------------------------


@app.get("/api/ib/search")
def search_ib(
    q: str = "",
    rtype: str | None = None,
    level: str | None = None,
    year: str | None = None,
    session: str | None = None,
    limit: int = 15,
) -> dict[str, Any]:
    try:
        res = ib_client.search_ib(
            q=q,
            rtype=rtype,
            level=level,
            year=year,
            session=session,
            limit=limit,
            pair_markschemes=True,
        )
        return {"status": "ok", "data": res}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/ib/facets")
def ib_facets(q: str = "", kind: str | None = None) -> dict[str, Any]:
    try:
        res = ib_client.get_ib_facets(q=q, kind=kind)
        return {"status": "ok", "data": res}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# --- Settings / Runtime Config -----------------------------------------------


@app.get("/api/settings")
def get_settings(request: Request, doomed_sid: str | None = Cookie(None)) -> dict[str, Any]:
    get_current_session(request, doomed_sid)
    cfg = config.get_llm_config()
    return {"status": "ok", "config": cfg}


class UpdateSettingsRequest(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    teacher_model: str | None = None
    maker_model: str | None = None
    researcher_model: str | None = None


@app.post("/api/settings")
def update_settings(
    body: UpdateSettingsRequest,
    request: Request,
    doomed_sid: str | None = Cookie(None),
) -> dict[str, Any]:
    get_current_session(request, doomed_sid)
    config.update_llm_config(
        base_url=body.base_url,
        api_key=body.api_key,
        teacher_model=body.teacher_model,
        maker_model=body.maker_model,
        researcher_model=body.researcher_model,
        persist=True,
    )
    return {"status": "ok", "config": config.get_llm_config()}


# --- Mount Frontend ----------------------------------------------------------

if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
