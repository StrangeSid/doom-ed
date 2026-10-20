# SPDX-License-Identifier: GPL-3.0-or-later
"""Pedagogy orchestrator and multi-turn agent execution loop.

Manages:
- Streaming OpenAI-compatible LLM requests with fallback.
- Dynamic tool registration: quiz, ask_user_question, create_visual, search_lms_files,
  browse_lms_folder, search_ib_resources, select_and_attach_files, read_document,
  search_within_document, inspect_document_pages, research_topic, confirm_lms_write.
- Turn pause and resumption for interactive cards (QuizCard, AskCard, WriteConfirmCard).
- Browser visual verification loop (DOM check + PNG canvas vision feedback).
- Safety guard: unsubmitted/live test lockout enforcement.
"""

from __future__ import annotations

import base64
import json
import random
import re
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, Generator, Iterator, List, Optional

import requests

from app import config, file_reader, ib_client, lms_bridge, prompts, research
from app.db import Database
from app.security import WriteConfirmationManager, assert_not_live_test, sanitize_filename
from app.sessions import Session


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "quiz",
            "description": "Pose ONE calibrated multiple-choice check to test the learner's understanding of the current node or bracket their knowledge edge. Single or multi-select. An 'I don't know' option is automatically added.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The exact question to ask."},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Display label for option."},
                                "value": {"type": "string", "description": "Machine-readable value or label."},
                                "description": {"type": "string", "description": "Optional extra detail."},
                            },
                            "required": ["label"],
                        },
                        "description": "2 to 5 options (do not include 'I don't know').",
                    },
                    "correct": {
                        "description": "0-based index (or list of indices for multiSelect) of the correct option in options array.",
                        "oneOf": [{"type": "integer"}, {"type": "array", "items": {"type": "integer"}}],
                    },
                    "explanation": {
                        "type": "string",
                        "description": "Clear explanation shown to learner immediately after grading.",
                    },
                    "multiSelect": {
                        "type": "boolean",
                        "description": "True if more than one option must be selected.",
                        "default": False,
                    },
                },
                "required": ["question", "options", "correct", "explanation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user_question",
            "description": "Ask an ungraded question (e.g. goal-setting in Phase 1 Part B, plan approval in Phase 2, or Socratic fork).",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to ask."},
                    "context": {"type": "string", "description": "Optional context shown above question."},
                    "mode": {
                        "type": "string",
                        "enum": ["single-select", "multi-select", "text"],
                        "default": "single-select",
                    },
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "value": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["label"],
                        },
                        "description": "Options if in select mode. 'Other (type your own)' is automatically appended.",
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_visual",
            "description": "Author ONE minimal, verified diagram (Mermaid for structure/relationships, SVG for spatial/geometry). Only use when a picture earns its place (<= 5-7 elements).",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["mermaid", "svg"], "description": "mermaid for dependency/flow/state, svg for spatial/geometry."},
                    "slug": {"type": "string", "description": "Short kebab-case topic name, e.g. 'vector-addition-triangle'."},
                    "brief": {"type": "string", "description": "Precise brief specifying the ONE idea and fewest elements."},
                },
                "required": ["kind", "slug", "brief"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_lms_files",
            "description": "Search the student's enrolled course materials, notes, slides, and task attachments via full-text search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword search across titles, filenames, and folder paths."},
                    "subject": {"type": "string", "description": "Optional subject filter."},
                    "ext": {"type": "string", "description": "Optional extension filter, e.g. 'pdf', 'docx'."},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse_lms_folder",
            "description": "Browse folder contents of an enrolled course or resource directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "course_id": {"type": "string", "description": "Course identifier."},
                    "folder_id": {"type": "string", "description": "Folder resource identifier."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_ib_resources",
            "description": "Search 53k+ IB resources (past papers, markschemes, questionbanks, subject notes). Past papers are automatically paired with their matching markscheme.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords, e.g. 'kinematics', 'physics paper 2'."},
                    "rtype": {"type": "string", "enum": ["paper", "markscheme", "questionbank", "notes", "textbook"]},
                    "level": {"type": "string", "enum": ["HL", "SL", "MYP"]},
                    "year": {"type": "string", "description": "e.g. '2023'"},
                    "session": {"type": "string", "enum": ["may", "november"]},
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_and_attach_files",
            "description": "Autonomously fetch and attach relevant LMS or IB file refs to the active chat thread for deep reading.",
            "parameters": {
                "type": "object",
                "properties": {
                    "refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of refs, e.g. ['lms://resource/10/20', 'ib://<sha256>'].",
                    },
                },
                "required": ["refs"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Read text content and diagram status of specific pages in an attached document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "File ref or SHA-256."},
                    "pages": {"type": "string", "description": "Page range, e.g. '1-3' or '4'."},
                },
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_within_document",
            "description": "Search for specific keywords or concepts across all pages of an attached file, returning snippets and page numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "File ref or SHA-256."},
                    "query": {"type": "string", "description": "Search terms to look for inside document."},
                    "top_k": {"type": "integer", "default": 5},
                },
                "required": ["ref", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_document_pages",
            "description": "Rasterize specific pages of an attached PDF or image into high-resolution images so you can visually inspect equations, graphs, circuit diagrams, or handwriting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "File ref or SHA-256."},
                    "pages": {
                        "description": "List of page numbers (e.g. [2, 3]) to visually render (max 6).",
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "dpi": {"type": "integer", "default": 150},
                },
                "required": ["ref", "pages"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research_topic",
            "description": "Search the web and fetch authoritative sources when verifying facts, syllabus details, or external concepts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_lms_write",
            "description": "Request student confirmation to perform an LMS write action (mark notification read, mark announcement read, send teacher message, request extension, submit task). Shows an interactive confirmation card.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "mark_notification_read",
                            "mark_announcement_read",
                            "send_message",
                            "request_extension",
                            "submit_task",
                        ],
                    },
                    "payload": {"type": "object", "description": "Action-specific parameters."},
                    "reason": {"type": "string", "description": "Clear human-readable description of what will be changed."},
                },
                "required": ["action", "payload", "reason"],
            },
        },
    },
]


_FILE_ROW_KEEP = ("ref", "title", "filename", "subject", "folder_path", "teacher", "ext")


def _clamp_int(value: Any, low: int, high: int, default: int | None = None) -> int:
    """Read an integer from a tool argument without trusting the model."""
    try:
        num = int(value)
    except (TypeError, ValueError):
        num = default if default is not None else low
    return max(low, min(num, high))


def _slim_file_rows(rows: Any) -> Any:
    """Trim raw LMS file rows to the fields a teacher actually needs.

    The full rows carry an `owner` key, timestamps, and a long base64 blob
    inside `lms://url/...` refs. Feeding those to the model wastes context
    and buries the useful metadata.
    """
    if not isinstance(rows, list):
        return rows
    out = []
    for row in rows[:25]:
        if not isinstance(row, dict):
            continue
        slim = {k: row.get(k) for k in _FILE_ROW_KEEP if row.get(k)}
        ref = slim.get("ref")
        if isinstance(ref, str) and ref.startswith("lms://url/"):
            slim["ref"] = "lms://url/<attachment>"
        out.append(slim)
    return out


def call_llm_chat(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
    temperature: float = 0.2,
    stream: bool = False,
) -> Any:
    """Call OpenAI-compatible chat completions API."""
    cfg = config.get_llm_config()
    base_url = cfg.get("base_url", "https://openrouter.ai/api/v1").rstrip("/")
    api_key = cfg.get("api_key", "")
    target_model = model or cfg.get("teacher_model", "google/gemini-2.5-pro")

    headers = {
        "Authorization": f"Bearer {api_key}" if api_key else "",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": target_model,
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
    }
    if tools:
        body["tools"] = tools

    url = f"{base_url}/chat/completions"
    resp = requests.post(url, headers=headers, json=body, timeout=180, stream=stream)
    resp.raise_for_status()
    if stream:
        return resp
    return resp.json()


def _stream_message(resp) -> Generator[str, None, dict[str, Any]]:
    """Read an OpenAI-compatible SSE stream.

    Yields each content delta as it arrives. The generator's return value
    (StopIteration.value) is the assembled message:
    ``{"content": str, "tool_calls": [...], "finish_reason": str}``.

    Tool call arguments arrive fragmented across chunks, so they are
    accumulated per index and parsed once at the end.
    """
    content_parts: list[str] = []
    tool_acc: dict[int, dict[str, Any]] = {}
    finish_reason = ""

    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        line = raw_line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            chunk = json.loads(line)
        except (TypeError, ValueError):
            continue

        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        finish_reason = choice.get("finish_reason") or finish_reason
        delta = choice.get("delta") or {}

        piece = delta.get("content")
        if piece:
            content_parts.append(piece)
            yield piece

        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = tool_acc.setdefault(idx, {"id": "", "type": "function", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]

    tool_calls: list[dict[str, Any]] = []
    for idx in sorted(tool_acc):
        slot = tool_acc[idx]
        if not slot.get("name"):
            continue
        tool_calls.append({
            "id": slot.get("id") or f"call_{idx}",
            "type": "function",
            "function": {"name": slot["name"], "arguments": slot.get("arguments") or "{}"},
        })

    return {
        "content": "".join(content_parts),
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
    }


class Orchestrator:
    """Executes the Socratic teaching loop, manages tool execution, pauses for interactive
    cards, handles visual maker verification, and streams SSE events to the browser."""

    def __init__(self, db: Database, write_mgr: WriteConfirmationManager) -> None:
        self.db = db
        self.write_mgr = write_mgr
        # In-memory storage for active visual maker sessions awaiting browser DOM verification
        self._pending_viz: dict[str, dict[str, Any]] = {}

    def process_user_turn(
        self,
        session: Session,
        thread_id: str,
        user_message: str,
        student_triage: dict[str, Any] | None = None,
        attached_refs: list[str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Process one user turn.

        Yields SSE events as they happen: prose tokens stream out while the
        model is still writing, tool calls report their start and finish,
        and interactive cards pause the loop until the student answers.
        """

        thread = self.db.get_thread(session.owner, thread_id)
        if not thread:
            thread = self.db.create_thread(
                owner=session.owner,
                title=user_message[:40] or "New Session",
                thread_id=thread_id,
            )

        # 1. Attach explicit files if provided
        if attached_refs:
            for r in attached_refs:
                try:
                    f_meta = file_reader.fetch_and_cache_ref(self.db, session.owner, r, session=session)
                    self.db.attach_file_to_thread(
                        thread_id=thread_id,
                        owner=session.owner,
                        ref=r,
                        sha256=f_meta["sha256"],
                        filename=f_meta.get("filename", ""),
                        attached_by="user",
                    )
                    yield {
                        "event": "file_attached",
                        "data": {
                            "ref": r,
                            "sha256": f_meta["sha256"],
                            "filename": f_meta.get("filename", ""),
                            "page_count": f_meta.get("page_count", 1),
                        },
                    }
                except Exception as e:
                    pass

        # 2. Add user message to DB
        self.db.add_message(
            thread_id=thread_id,
            role="user",
            content=user_message,
        )
        yield {"event": "user_message", "data": {"content": user_message}}

        # 3. Build system prompt & message history
        active_attachments = self.db.list_thread_attachments(thread_id)
        system_prompt = prompts.build_teacher_system_prompt(
            student_context=student_triage,
            thread_phase=thread.get("phase", "probe"),
            attached_files=active_attachments,
        )

        db_msgs = self.db.list_messages(thread_id)
        llm_messages = [{"role": "system", "content": system_prompt}]
        for m in db_msgs:
            msg_dict: dict[str, Any] = {"role": m["role"], "content": m["content"]}
            if m.get("tool_calls_json"):
                try:
                    msg_dict["tool_calls"] = json.loads(m["tool_calls_json"])
                except Exception:
                    pass
            if m.get("tool_call_id"):
                msg_dict["tool_call_id"] = m["tool_call_id"]
            llm_messages.append(msg_dict)

        # 4. Agent loop (up to 12 autonomous tool turns)
        # Repeating the same tool call with the same arguments returns the same
        # result, so we track signatures and short-circuit duplicates. Without
        # this the model can burn every turn re-running one search.
        seen_tool_calls: set[str] = set()
        for turn_idx in range(12):
            cfg = config.get_llm_config()
            teacher_model = cfg.get("teacher_model", "agy/gemini-3.8-flash-high")

            # Stream the completion so the student sees prose while the model is
            # still writing instead of staring at a blank bubble for 30s.
            resp = call_llm_chat(
                messages=llm_messages,
                tools=TOOL_DEFINITIONS,
                model=teacher_model,
                temperature=0.2,
                stream=True,
            )
            stream = _stream_message(resp)
            try:
                while True:
                    piece = next(stream)
                    yield {"event": "token", "data": {"content": piece}}
            except StopIteration as stop:
                message = stop.value or {}
            except Exception:
                message = {}

            content = message.get("content") or ""
            tool_calls = message.get("tool_calls") or []

            # Save assistant message
            self.db.add_message(
                thread_id=thread_id,
                role="assistant",
                content=content,
                tool_calls=tool_calls if tool_calls else None,
            )

            if not tool_calls:
                break

            # Handle tool calls
            paused_for_card = False
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_args = {}
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except Exception:
                    pass
                call_id = tc["id"]

                # Duplicate guard: identical tool + identical args yields the same
                # answer. Answer it once and tell the model it already has it.
                signature = f"{fn_name}:{json.dumps(fn_args, sort_keys=True, default=str)}"
                if signature in seen_tool_calls:
                    yield self._append_tool_result(
                        llm_messages,
                        thread_id,
                        call_id,
                        {
                            "status": "duplicate",
                            "note": "This exact call already ran earlier in this turn. Use the earlier result instead of repeating it.",
                        },
                        fn_name,
                    )
                    continue
                seen_tool_calls.add(signature)

                yield {
                    "event": "tool_start",
                    "data": {"tool_name": fn_name, "call_id": call_id, "args": fn_args},
                }

                # A. QUIZ CARD (PAUSES LOOP)
                if fn_name == "quiz":
                    question = fn_args.get("question", "")
                    raw_options = fn_args.get("options", [])
                    raw_correct = fn_args.get("correct", 0)
                    explanation = fn_args.get("explanation", "")
                    multi_select = bool(fn_args.get("multiSelect", False))

                    # Shuffle options, keep track of new correct index, pin "I don't know" at index 0
                    real_options = list(raw_options)
                    indexed_options = list(enumerate(real_options))
                    random.shuffle(indexed_options)

                    new_options = []
                    correct_indices: list[int] = []

                    target_correct_set = set(raw_correct if isinstance(raw_correct, list) else [raw_correct])

                    for new_idx, (orig_idx, opt) in enumerate(indexed_options, start=1):
                        new_options.append({
                            "index": new_idx,
                            "label": opt.get("label", ""),
                            "value": opt.get("value", opt.get("label", "")),
                            "description": opt.get("description", ""),
                        })
                        if orig_idx in target_correct_set:
                            correct_indices.append(new_idx)

                    # Create quiz state in DB (correct answer held strictly server-side)
                    self.db.create_quiz_state(
                        call_id=call_id,
                        thread_id=thread_id,
                        owner=session.owner,
                        question=question,
                        options=new_options,
                        correct=correct_indices,
                        explanation=explanation,
                        multi_select=multi_select,
                    )

                    card_data = {
                        "card_type": "quiz",
                        "call_id": call_id,
                        "question": question,
                        "options": new_options,
                        "multi_select": multi_select,
                        "answered": False,
                    }
                    yield {"event": "card", "data": card_data}
                    paused_for_card = True

                # B. ASK USER QUESTION CARD (PAUSES LOOP)
                elif fn_name == "ask_user_question":
                    card_data = {
                        "card_type": "ask",
                        "call_id": call_id,
                        "question": fn_args.get("question", ""),
                        "context": fn_args.get("context", ""),
                        "mode": fn_args.get("mode", "single-select"),
                        "options": fn_args.get("options", []),
                        "answered": False,
                    }
                    yield {"event": "card", "data": card_data}
                    paused_for_card = True

                # C. WRITE CONFIRM CARD (PAUSES LOOP)
                elif fn_name == "confirm_lms_write":
                    action = fn_args.get("action", "")
                    payload = fn_args.get("payload", {})
                    reason = fn_args.get("reason", "")
                    tok_dict = lms_bridge.request_write_confirmation(
                        self.write_mgr, session, action, payload
                    )
                    card_data = {
                        "card_type": "write_confirm",
                        "token_id": tok_dict["token_id"],
                        "action": action,
                        "payload": payload,
                        "reason": reason,
                        "expires_at": tok_dict["expires_at"],
                    }
                    yield {"event": "card", "data": card_data}
                    paused_for_card = True

                # D. CREATE VISUAL (MAKER SUBAGENT LOOP)
                elif fn_name == "create_visual":
                    kind = fn_args.get("kind", "mermaid")
                    slug = fn_args.get("slug", "diagram")
                    brief = fn_args.get("brief", "")
                    yield self._start_visual_maker(
                        session=session,
                        thread_id=thread_id,
                        call_id=call_id,
                        kind=kind,
                        slug=slug,
                        brief=brief,
                    )
                    # Tool completes or requests browser render
                    paused_for_card = True

                # E. SEARCH LMS FILES
                elif fn_name == "search_lms_files":
                    q = fn_args.get("query", "")
                    sub = fn_args.get("subject")
                    ext = fn_args.get("ext")
                    lim = _clamp_int(fn_args.get("limit", 10), 1, 25)
                    res = lms_bridge.search_files(session, self.db, q, subject=sub, ext=ext, limit=lim)
                    yield self._append_tool_result(
                        llm_messages, thread_id, call_id, _slim_file_rows(res), fn_name
                    )

                # F. BROWSE LMS FOLDER
                elif fn_name == "browse_lms_folder":
                    cid = fn_args.get("course_id")
                    fid = fn_args.get("folder_id")
                    res = lms_bridge.browse_lms_folder(session, self.db, course_id=cid, folder_id=fid)
                    yield self._append_tool_result(llm_messages, thread_id, call_id, res, fn_name)

                # G. SEARCH IB RESOURCES
                elif fn_name == "search_ib_resources":
                    q = fn_args.get("query", "")
                    rtype = fn_args.get("rtype")
                    lvl = fn_args.get("level")
                    yr = fn_args.get("year")
                    sess_term = fn_args.get("session")
                    lim = _clamp_int(fn_args.get("limit", 8), 1, 25)
                    res = ib_client.search_ib(
                        q=q,
                        rtype=rtype,
                        level=lvl,
                        year=yr,
                        session=sess_term,
                        limit=lim,
                        pair_markschemes=True,
                    )
                    yield self._append_tool_result(llm_messages, thread_id, call_id, res, fn_name)

                # H. SELECT AND ATTACH FILES
                elif fn_name == "select_and_attach_files":
                    refs = fn_args.get("refs", [])
                    attached = []
                    for r in refs:
                        try:
                            f_meta = file_reader.fetch_and_cache_ref(self.db, session.owner, r, session=session)
                            self.db.attach_file_to_thread(
                                thread_id=thread_id,
                                owner=session.owner,
                                ref=r,
                                sha256=f_meta["sha256"],
                                filename=f_meta.get("filename", ""),
                                attached_by="agent",
                            )
                            attached.append({
                                "ref": r,
                                "sha256": f_meta["sha256"],
                                "filename": f_meta.get("filename", ""),
                                "page_count": f_meta.get("page_count", 1),
                            })
                            yield {
                                "event": "file_attached",
                                "data": {
                                    "ref": r,
                                    "sha256": f_meta["sha256"],
                                    "filename": f_meta.get("filename", ""),
                                    "page_count": f_meta.get("page_count", 1),
                                },
                            }
                        except Exception as e:
                            pass
                    yield self._append_tool_result(
                        llm_messages, thread_id, call_id, {"attached": attached}, fn_name
                    )

                # I. READ DOCUMENT
                elif fn_name == "read_document":
                    ref = fn_args.get("ref", "")
                    pgs = fn_args.get("pages", "1-5")
                    res = file_reader.read_document(self.db, session.owner, ref, pages=pgs, session=session)
                    yield self._append_tool_result(llm_messages, thread_id, call_id, res, fn_name)

                # J. SEARCH WITHIN DOCUMENT
                elif fn_name == "search_within_document":
                    ref = fn_args.get("ref", "")
                    q = fn_args.get("query", "")
                    top_k = _clamp_int(fn_args.get("top_k", 5), 1, 25)
                    res = file_reader.search_within_document(self.db, session.owner, ref, q, top_k=top_k, session=session)
                    yield self._append_tool_result(llm_messages, thread_id, call_id, res, fn_name)

                # K. INSPECT DOCUMENT PAGES (MULTIMODAL)
                elif fn_name == "inspect_document_pages":
                    ref = fn_args.get("ref", "")
                    pgs = fn_args.get("pages", [1])
                    dpi = _clamp_int(fn_args.get("dpi", 150), 72, 220)
                    renders = file_reader.inspect_document_pages(self.db, session.owner, ref, pages=pgs, dpi=dpi, session=session)
                    # Format as multimodal tool response
                    tool_content: list[dict[str, Any]] = [
                        {"type": "text", "text": f"Inspected pages {pgs} of {ref}:"}
                    ]
                    for r in renders:
                        if "data_url" in r:
                            tool_content.append({
                                "type": "image_url",
                                "image_url": {"url": r["data_url"]},
                            })
                    self.db.add_message(
                        thread_id=thread_id,
                        role="tool",
                        content=json.dumps({"rendered_pages": len(renders)}),
                        tool_call_id=call_id,
                    )
                    llm_messages.append({"role": "tool", "content": tool_content, "tool_call_id": call_id})
                    yield {
                        "event": "tool_done",
                        "data": {"call_id": call_id, "rendered_count": len(renders)},
                    }

                # L. RESEARCH TOPIC
                elif fn_name == "research_topic":
                    q = fn_args.get("query", "")
                    findings = research.web_search(q, max_results=4)
                    yield self._append_tool_result(
                        llm_messages, thread_id, call_id, {"search_results": findings}, fn_name
                    )

                else:
                    yield self._append_tool_result(
                        llm_messages, thread_id, call_id, {"error": f"Unknown tool: {fn_name}"}, fn_name
                    )

            if paused_for_card:
                # Stop turn and wait for user card interaction
                break

        yield {"event": "done", "data": {"thread_id": thread_id}}

    def _append_tool_result(
        self,
        llm_messages: list[dict[str, Any]],
        thread_id: str,
        call_id: str,
        result: Any,
        tool_name: str,
    ) -> dict[str, Any]:
        """Record a tool result in the thread and return the event to emit."""
        res_str = json.dumps(result, default=str)
        self.db.add_message(
            thread_id=thread_id,
            role="tool",
            content=res_str,
            tool_call_id=call_id,
        )
        llm_messages.append({"role": "tool", "content": res_str, "tool_call_id": call_id})
        return {
            "event": "tool_done",
            "data": {"tool_name": tool_name, "call_id": call_id, "result": result},
        }

    def _start_visual_maker(
        self,
        session: Session,
        thread_id: str,
        call_id: str,
        kind: str,
        slug: str,
        brief: str,
    ) -> dict[str, Any]:
        """Call specialist maker subagent to generate initial diagram source code,
        then emit viz_render_request to browser for DOM validation and PNG rasterization."""
        cfg = config.get_llm_config()
        maker_model = cfg.get("maker_model", "agy/gemini-3.8-flash-high")

        system_prompt = prompts.MERMAID_MAKER_PROMPT if kind == "mermaid" else prompts.SVG_MAKER_PROMPT
        maker_msgs = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"Brief: {brief}\nSlug: {slug}\nProduce the initial diagram source code. Return ONLY the code inside ```{kind} ... ```.",
            },
        ]

        res = call_llm_chat(messages=maker_msgs, model=maker_model, temperature=0.1)
        raw_reply = res["choices"][0]["message"].get("content", "")

        # Extract code block
        match = re.search(r"```(?:mermaid|svg)?\s*([\s\S]*?)```", raw_reply)
        source_code = match.group(1).strip() if match else raw_reply.strip()

        # Track pending visual
        self._pending_viz[call_id] = {
            "thread_id": thread_id,
            "owner": session.owner,
            "kind": kind,
            "slug": slug,
            "brief": brief,
            "source_code": source_code,
            "iteration": 1,
            "maker_messages": maker_msgs,
        }

        # Ask browser to validate DOM & rasterize
        return {
            "event": "viz_render_request",
            "data": {
                "call_id": call_id,
                "kind": kind,
                "slug": slug,
                "source": source_code,
            },
        }

    def submit_card_reply(
        self,
        session: Session,
        thread_id: str,
        card_id: str,
        reply_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Submit answer to QuizCard, AskCard, or WriteConfirmCard, and resume teacher loop."""
        # The thread id arrives from the browser, so confirm the caller owns it
        # before writing anything into the conversation.
        if self.db.get_thread(session.owner, thread_id) is None:
            raise PermissionError("Unknown lesson thread.")

        # 1. QUIZ CARD SUBMISSION
        quiz = self.db.get_quiz_state(card_id, owner=session.owner)
        if quiz:
            selected = reply_data.get("selected")
            sel_list = selected if isinstance(selected, list) else [selected]

            try:
                correct_indices = json.loads(quiz.get("correct_json") or "[]")
            except (TypeError, ValueError):
                correct_indices = []
            if not isinstance(correct_indices, list):
                correct_indices = [correct_indices]
            correct_indices = [int(i) for i in correct_indices if str(i).lstrip("-").isdigit()]

            is_dont_know = sel_list == [0] or sel_list == ["__dont_know__"]
            correct_set = set(correct_indices)

            is_correct = False
            if not is_dont_know:
                is_correct = set(sel_list) == correct_set

            grade_result = {
                "correct": is_correct,
                "dontKnow": is_dont_know,
                "correct_options": correct_indices,
                "explanation": quiz.get("explanation"),
            }

            self.db.record_quiz_answer(
                call_id=card_id,
                owner=session.owner,
                user_answer=sel_list,
                grade_result=grade_result,
            )

            # Record tool response in thread
            tool_resp = {
                "status": "answered",
                "correct": is_correct,
                "dontKnow": is_dont_know,
                "selected": sel_list,
                "correct_indices": correct_indices,
                "explanation": quiz.get("explanation"),
            }
            self.db.add_message(
                thread_id=thread_id,
                role="tool",
                content=json.dumps(tool_resp),
                tool_call_id=card_id,
            )

            return {
                "status": "ok",
                "card_type": "quiz",
                "grade": grade_result,
            }

        # 2. WRITE CONFIRMATION SUBMISSION
        if "confirm" in reply_data:
            if reply_data.get("confirm") is True:
                write_res = lms_bridge.execute_confirmed_write(
                    self.write_mgr, session, card_id
                )
                self.db.add_message(
                    thread_id=thread_id,
                    role="tool",
                    content=json.dumps({"confirmed": True, "result": write_res}),
                    tool_call_id=card_id,
                )
                return {"status": "ok", "card_type": "write_confirm", "result": write_res}
            else:
                self.db.add_message(
                    thread_id=thread_id,
                    role="tool",
                    content=json.dumps({"confirmed": False, "reason": "User cancelled write."}),
                    tool_call_id=card_id,
                )
                return {"status": "cancelled", "card_type": "write_confirm"}

        # 3. ASK USER QUESTION SUBMISSION
        answer = reply_data.get("answer") or reply_data.get("selected")
        self.db.add_message(
            thread_id=thread_id,
            role="tool",
            content=json.dumps({"status": "answered", "answer": answer}),
            tool_call_id=card_id,
        )
        return {"status": "ok", "card_type": "ask", "answer": answer}

    def handle_viz_feedback(
        self,
        session: Session,
        call_id: str,
        feedback: dict[str, Any],
    ) -> dict[str, Any]:
        """Browser returns DOM verification check (warnings/errors) + PNG data URL.
        Maker model inspects visual, either edits source or publishes into chat."""
        pending = self._pending_viz.get(call_id)
        if not pending:
            return {"status": "error", "message": "Unknown visual call_id."}
        if pending.get("owner") != session.owner:
            return {"status": "error", "message": "Unknown visual call_id."}

        ok = feedback.get("ok", True)
        dom_warnings = feedback.get("dom_warnings", [])
        png_data_url = feedback.get("png_data_url")
        error_msg = feedback.get("error")

        thread_id = pending["thread_id"]
        kind = pending["kind"]
        slug = pending["slug"]
        source = pending["source_code"]

        # If clean or iteration limit reached, publish visual
        if (ok and not dom_warnings) or pending["iteration"] >= 2 or error_msg:
            # Save visual in DB
            viz_id = str(uuid.uuid4())
            data_dir = config.ensure_data_dir()
            viz_dir = data_dir / "viz" / thread_id
            viz_dir.mkdir(parents=True, exist_ok=True)

            png_path = None
            if png_data_url and "," in png_data_url:
                try:
                    b64_raw = png_data_url.split(",", 1)[1]
                    png_bytes = base64.b64decode(b64_raw)
                    # The slug comes from the model. A crafted value like
                    # "../../.." would otherwise write outside data/.
                    safe_slug = sanitize_filename(slug)[:60] or "diagram"
                    if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise ValueError("not a PNG")
                    if len(png_bytes) > 8 * 1024 * 1024:
                        raise ValueError("PNG too large")
                    p_file = viz_dir / f"{safe_slug}-{viz_id[:8]}.png"
                    if p_file.resolve().parent != viz_dir.resolve():
                        raise ValueError("path escapes the visual directory")
                    p_file.write_bytes(png_bytes)
                    png_path = str(p_file)
                except Exception:
                    pass

            self.db.save_visual(
                thread_id=thread_id,
                owner=session.owner,
                kind=kind,
                slug=slug,
                brief=pending["brief"],
                source_code=source,
                svg_data=source if kind == "svg" else None,
                png_path=png_path,
                verified=1 if ok else 0,
                dom_warnings=dom_warnings,
                visual_id=viz_id,
            )

            # Inform teacher agent
            tool_resp = {
                "status": "published",
                "visual_id": viz_id,
                "kind": kind,
                "slug": slug,
                "download_url": f"/api/viz/{viz_id}/png",
            }
            self.db.add_message(
                thread_id=thread_id,
                role="tool",
                content=json.dumps(tool_resp),
                tool_call_id=call_id,
            )

            del self._pending_viz[call_id]
            return {
                "status": "published",
                "visual_id": viz_id,
                "kind": kind,
                "slug": slug,
                "png_url": f"/api/viz/{viz_id}/png",
            }

        else:
            # Maker edit loop
            pending["iteration"] += 1
            cfg = config.get_llm_config()
            maker_model = cfg.get("maker_model", "google/gemini-2.5-flash")

            inspect_msg: dict[str, Any] = {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"DOM layout warnings detected: {dom_warnings}\nInspect this rendered preview and fix overlapping or cramped labels. Return ONLY the revised ```{kind} ... ``` block.",
                    }
                ],
            }
            if png_data_url:
                inspect_msg["content"].append({
                    "type": "image_url",
                    "image_url": {"url": png_data_url},
                })

            msgs = pending["maker_messages"] + [
                {"role": "assistant", "content": f"```{kind}\n{source}\n```"},
                inspect_msg,
            ]

            res = call_llm_chat(messages=msgs, model=maker_model, temperature=0.1)
            raw_reply = res["choices"][0]["message"].get("content", "")
            match = re.search(r"```(?:mermaid|svg)?\s*([\s\S]*?)```", raw_reply)
            new_source = match.group(1).strip() if match else raw_reply.strip()

            pending["source_code"] = new_source
            pending["maker_messages"] = msgs

            return {
                "status": "retry_render",
                "call_id": call_id,
                "kind": kind,
                "slug": slug,
                "source": new_source,
            }
