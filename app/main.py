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

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
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

logger = logging.getLogger("doomed")

app = FastAPI(
    title="doom-ed",
    description="AI Study Coach & Pedagogy System grounded in Indus LMS and IB Resources",
    version="0.1.0",
)

# CORS is a same-origin app. A wildcard with credentials lets any site try
# authenticated requests, so only the configured origins are allowed.
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("DOOMED_ALLOWED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
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


def _is_loopback(request: Request) -> bool:
    """True when the request came from the machine itself."""
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def get_current_session(request: Request, doomed_sid: str | None = Cookie(None)) -> Session:
    """Retrieve the authenticated session.

    Falls back to bootstrapping from the local Indus token file, but only when
    the operator opted in AND the request came from loopback. Without that
    guard any process on the machine (or a tunnel) could read the student's data.
    """
    sid = doomed_sid or request.headers.get("x-session-id")
    sess = session_store.get(sid) if sid else None
    if not sess and os.environ.get("DOOMED_AUTO_BOOTSTRAP_TOKEN", "0") == "1" and _is_loopback(request):
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
    profile_error: str | None = None
    try:
        payload = lms_bridge.fetch_student_payload(sess, session_store, refresh=False)
        if isinstance(payload.get("student"), dict):
            student_info.update(payload["student"])
        if payload.get("_auth_error"):
            profile_error = payload["_auth_error"]
    except lms_bridge.LMSNotAuthenticatedError as e:
        profile_error = str(e)
    except Exception as e:
        profile_error = str(e)

    body: dict[str, Any] = {
        "status": "ok",
        "student": student_info,
    }
    if profile_error:
        body["profile_error"] = profile_error
    return body


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
        body: dict[str, Any] = {"status": "ok", "data": triage}
        # The triage may have been built from the last known payload because
        # the Indus token expired. Say so instead of pretending it is live.
        if triage.pop("_auth_error", None):
            body["auth_error"] = "Your Indus LMS session expired. Sign in to refresh your data."
        return body
    except lms_bridge.LMSNotAuthenticatedError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your Indus LMS session expired. Sign in again.",
        )
    except lms_bridge.LMSUpstreamError:
        # The upstream message can name internal ids and URLs. Log it, but
        # return something the browser can show without leaking internals.
        logger.exception("Indus LMS upstream failure while building the study triage")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Indus LMS did not respond. Try again shortly.",
        )
    except Exception:
        logger.exception("Unexpected failure while building the study triage")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not build the study briefing. Try again shortly.",
        )


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
            # The orchestrator is a blocking generator (LLM calls, file reads).
            # Run it in a worker thread and push events into the SSE stream as
            # they are produced, so prose streams to the browser live.
            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            sentinel = object()

            def _pump() -> None:
                try:
                    for ev in orchestrator.process_user_turn(
                        session=sess,
                        thread_id=thread_id,
                        user_message=body.message,
                        student_triage=triage,
                        attached_refs=body.attached_refs,
                    ):
                        loop.call_soon_threadsafe(queue.put_nowait, ev)
                except Exception as e:  # surfaced to the browser, not swallowed
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {"event": "error", "data": {"error": str(e)}},
                    )
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, sentinel)

            threading.Thread(target=_pump, daemon=True).start()

            while True:
                ev = await queue.get()
                if ev is sentinel:
                    break
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


def _sniff_mime(raw: bytes, declared: str, filename: str) -> str:
    """Determine the served MIME type from the bytes themselves.

    A declared `text/html` on an uploaded file would otherwise be served
    inline from our own origin and run as same-origin script. Only a small
    allowlist is served inline; everything else becomes an attachment.
    """
    import mimetypes

    sniffed = ""
    if raw.startswith(b"%PDF-"):
        sniffed = "application/pdf"
    elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
        sniffed = "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        sniffed = "image/jpeg"
    elif raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a"):
        sniffed = "image/gif"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        sniffed = "image/webp"
    elif raw.lstrip()[:5] in (b"<?xml", b"<svg") and b"<svg" in raw[:1024].lower():
        sniffed = "image/svg+xml"

    if sniffed:
        return sniffed

    # Plain text is safe to serve inline, but only when the bytes really are
    # text and the declared type agrees.
    guessed, _ = mimetypes.guess_type(filename or "")
    if guessed and guessed.startswith("text/") and _looks_like_text(raw):
        return guessed
    if (declared or "").lower().startswith("text/plain") and _looks_like_text(raw):
        return "text/plain"
    return "application/octet-stream"


def _looks_like_text(raw: bytes) -> bool:
    """True when the bytes decode cleanly and hold no NUL or control bytes."""
    if b"\x00" in raw[:4096]:
        return False
    try:
        sample = raw[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return not any(ord(ch) < 9 or 13 < ord(ch) < 32 for ch in sample)


# Files that may be rendered inline in the browser. Everything else is sent as
# an attachment so the browser never executes it against our origin.
_INLINE_MIME = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
    "application/xml",
    "text/xml",
}


def _safe_filename(name: str) -> str:
    """A filename safe to put in a Content-Disposition header."""
    import re as _re

    clean = _re.sub(r'[\x00-\x1f\x7f";]', "", (name or "file").replace("\\", "/").rsplit("/", 1)[-1])
    return (clean[:200] or "file")


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
    fname = _safe_filename(file_row.get("filename") or f"{sha256}.bin")
    # The declared type is not trusted. Serve inline only what we recognise as
    # inert; everything else becomes an attachment the browser downloads.
    mime = _sniff_mime(raw_bytes, file_row.get("mime") or "", fname)
    inline = mime in _INLINE_MIME
    if not inline:
        mime = "application/octet-stream"
    disposition = "inline" if inline else "attachment"

    common_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": mime,
        "Cache-Control": "private, no-cache",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; sandbox",
    }

    # Support HTTP Range requests for PDF.js page streaming
    if range and range.startswith("bytes="):
        range_spec = range[6:].strip()
        parts = range_spec.split("-", 1)
        try:
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if parts[1] else total_len - 1
        except ValueError:
            raise HTTPException(status_code=416, detail="Malformed Range header.")
        start = max(0, min(start, total_len - 1))
        end = max(start, min(end, total_len - 1))
        chunk_len = end - start + 1
        headers = {
            **common_headers,
            "Content-Range": f"bytes {start}-{end}/{total_len}",
            "Content-Length": str(chunk_len),
            "Content-Disposition": f'{disposition}; filename="{fname}"',
        }
        return Response(content=raw_bytes[start : end + 1], status_code=206, headers=headers)

    headers = {
        **common_headers,
        "Content-Length": str(total_len),
        "Content-Disposition": f'{disposition}; filename="{fname}"',
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


def _public_config(cfg: dict[str, str]) -> dict[str, Any]:
    """Config shape safe to send to the browser.

    The API key never leaves the server. The browser only needs to know
    whether one is set, and which models are in use.
    """
    key = cfg.get("api_key") or ""
    return {
        "base_url": cfg.get("base_url", ""),
        "api_key_set": bool(key),
        "api_key_hint": f"...{key[-4:]}" if len(key) >= 4 else ("set" if key else ""),
        "teacher_model": cfg.get("teacher_model", ""),
        "maker_model": cfg.get("maker_model", ""),
        "researcher_model": cfg.get("researcher_model", ""),
    }


def _require_admin(request: Request) -> None:
    """Model routing is server-side configuration, not a student setting.

    Only a request from the machine itself may change it.
    """
    if not _is_loopback(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Model routing is configured on the server.",
        )


@app.get("/api/settings")
def get_settings(request: Request, doomed_sid: str | None = Cookie(None)) -> dict[str, Any]:
    get_current_session(request, doomed_sid)
    return {"status": "ok", "config": _public_config(config.get_llm_config())}


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
    _require_admin(request)
    config.update_llm_config(
        base_url=body.base_url,
        api_key=body.api_key,
        teacher_model=body.teacher_model,
        maker_model=body.maker_model,
        researcher_model=body.researcher_model,
        persist=True,
    )
    return {"status": "ok", "config": _public_config(config.get_llm_config())}


# --- Mount Frontend ----------------------------------------------------------

if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
