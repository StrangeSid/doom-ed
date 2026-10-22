# API and tool reference

Reference documentation for the HTTP endpoints, agent tools, and event stream in **doom-ed**.

---

## 1. HTTP endpoints

### 1.1 Authentication and sessions

| Endpoint | Method | Body / Params | Description |
|---|---|---|---|
| `/api/session/local` | `POST` | `{"name": "...", "grade": "...", "subjects": [...]}` | Creates standalone independent IB session (zero LMS credentials required). Sets HttpOnly `doomed_sid` cookie. |
| `/api/profile` | `POST` | `{"name": "...", "grade": "...", "subjects": [...], "target_session": "..."}` | Updates local IB student profile settings (subjects, grade, target exam session). |
| `/api/login` | `POST` | `{"email": "...", "password": "..."}` | Optional connector: authenticates against Indus LMS, links school account, and sets HttpOnly `doomed_sid` cookie. |
| `/api/logout` | `POST` | — | Clears server-side session and deletes cookie. |
| `/api/session` | `GET` | — | Returns student profile (`name`, `grade`, `subjects`, `target_session`, `is_lms_connected`). Returns `401` if no session cookie exists. |

---

### 1.2 Study coach triage

| Endpoint | Method | Body / Params | Description |
|---|---|---|---|
| `/api/study` | `GET` | `refresh=true` (optional) | Returns 3-question student triage: `now` (diagnostic papers, exam countdown, or overdue school work if LMS linked), `weak_topics` (Socratic quiz misses and syllabus fix list), and `before_break` (IB exam milestone queue or upcoming school deadlines). Works 100% standalone. |

---

### 1.3 Chat and pedagogy sessions

| Endpoint | Method | Body / Params | Description |
|---|---|---|---|
| `/api/threads` | `GET` | — | Lists student's lesson threads ordered by last updated. |
| `/api/threads` | `POST` | `{"title": "...", "topic": "...", "subject": "..."}` | Creates a new lesson thread. |
| `/api/threads/{thread_id}` | `GET` | — | Retrieves thread metadata, full message history, and attached files. |
| `/api/threads/{thread_id}` | `DELETE` | — | Deletes a lesson thread and its messages. |
| `/api/chat/send` | `POST` | `{"thread_id": "...", "message": "...", "attached_refs": [...]}` | Initiates multi-turn Socratic response. Streams SSE events: `token`, `tool_start`, `tool_done`, `card`, `file_attached`, `viz_render_request`, `done`, `error`. |
| `/api/chat/card-reply` | `POST` | `{"thread_id": "...", "card_id": "...", "reply": {...}}` | Submits student answer to `<QuizCard>`, `<AskCard>`, or `<WriteConfirmCard>`, records grade in lesson log, and resumes teacher loop. |
| `/api/chat/viz-feedback` | `POST` | `{"call_id": "...", "feedback": {"ok": true, "dom_warnings": [...], "png_data_url": "..."}}` | Browser posts DOM validation warnings and canvas PNG data URL for visual maker verification loop. |

---

### 1.4 Files, viewer and blobs

| Endpoint | Method | Body / Params | Description |
|---|---|---|---|
| `/api/files/{sha256}` | `GET` | Supports HTTP `Range: bytes=...` | Serves decrypted file bytes with `Accept-Ranges`, `Content-Length`, and `Content-Type` for the in-app slide-over viewer (`viewer.js`). Gated by strict per-owner ACL (`can_access_file`). |
| `/api/upload` | `POST` | `multipart/form-data` with `file` | Uploads student file, Fernet-encrypts it on disk, indexes text/pages, and attaches to active thread. |
| `/api/viz/{visual_id}/png` | `GET` | — | Serves rasterized PNG of published visual diagram. |

---

### 1.5 IB resource catalog proxy

| Endpoint | Method | Body / Params | Description |
|---|---|---|---|
| `/api/ib/search` | `GET` | `q`, `rtype`, `level`, `year`, `session`, `limit` | Ranked full-text search across 53k+ IB resources with automatic paper $\leftrightarrow$ markscheme pairing. |
| `/api/ib/facets` | `GET` | `q`, `kind` | Resource aggregation counts. |
| `/api/ib/resources/{sha256}` | `GET` | — | Metadata record for specific IB resource. |

---

### 1.6 Runtime settings

Model routing is server-side configuration. The browser never sends a provider or a key, and the API key never leaves the server.

| Endpoint | Method | Body / Params | Description |
|---|---|---|---|
| `/api/settings` | `GET` | — | Returns the active routing: `base_url`, `teacher_model`, `maker_model`, `researcher_model`, and `api_key_set` (a boolean). The key itself is never returned. |
| `/api/settings` | `POST` | `{"base_url": "...", "api_key": "...", "teacher_model": "..."}` | Updates routing and writes it to `config.json`. Only accepted from a loopback request. |

---

## 1.7 Health and error semantics

| Endpoint | Method | Description |
|---|---|---|
| `/health` | `GET` | Liveness probe. Returns `{"status": "ok", "app": "doom-ed", "version": "0.1.0"}`. No auth required. |

Error codes the browser handles:

| Code | Meaning | Client behaviour |
|---|---|---|
| `401` | No session, expired session, or the Indus LMS token is invalid. | Sign-in prompt, and a toast naming the expired session. |
| `403` | The student tried to read a file outside their `owner` scope, or asked about a live/unsubmitted test. | Message shown, request refused. |
| `502` | Indus LMS or the IB catalog failed upstream. | Toast with the upstream detail. |

`GET /api/session` returns `200` with a `profile_error` field when the session is valid but the LMS profile call failed. So the shell still renders while the student sees what is missing.

---

## 1.8 Expired Indus token

The Indus LMS access token expires on its own schedule, independently of the doom-ed session. When that happens:

1. The last known payload stays on screen. It's the student's own data, and an empty page helps nobody.
2. The response carries an `auth_error` field, and a banner appears above the chat with a **"Sign in again"** action. It stays up until a successful sign-in, so stale numbers are never mistaken for live ones.
3. The stale copy is only served inside the persistent cache window (one hour by default). Past that the API returns `401` and the student must sign in.
4. A successful sign-in clears the banner and reloads the triage.

---

## 1.9 SSE event stream

`POST /api/chat/send` returns `text/event-stream`. Frames are separated by a blank line.

| Event | Data | Meaning |
|---|---|---|
| `thread_created` | `{thread_id, thread}` | Emitted first when the request had no `thread_id`. |
| `user_message` | `{content}` | Echo of the accepted prompt. |
| `token` | `{content}` | A chunk of teacher prose. Frames arrive as the model writes, so the bubble fills progressively. |
| `tool_start` | `{tool_name, call_id, args}` | The agent began a tool call. |
| `tool_done` | `{tool_name, call_id, result}` | The tool finished. |
| `card` | card payload | An interactive card: `quiz`, `ask`, or `write_confirm`. The turn pauses until the student replies. |
| `file_attached` | `{ref, sha256, filename, page_count}` | A file was attached to the thread. |
| `viz_render_request` | `{call_id, kind, slug, source}` | The browser must render the diagram, validate it, and POST the result to `/api/chat/viz-feedback`. |
| `done` | `{thread_id}` | The turn finished. |
| `error` | `{error}` | Something failed. The message is shown as a toast. |

---

## 2. Agent tool reference

The teacher agent has 12 specialized tools:

1. **`quiz`**: Poses ONE calibrated multiple-choice check to bracket knowledge or test node understanding. The server shuffles the real options and reassigns indices 1..N; the browser renders a pinned `0: "I don't know"` row above them. The correct answer and explanation stay server-side until the student submits.
2. **`ask_user_question`**: Poses an ungraded question for goal-setting (Phase 1 Part B) or dependency-graph approval (Phase 2). The browser appends an "Other (type your own)" field.
3. **`create_visual`**: Dispatches `mermaid-maker` or `svg-maker` with a pruned brief ($\le 5\text{–}7$ elements) for web-native visual authoring.
4. **`search_lms_files`**: FTS5 search across the student's enrolled course materials, notes, slides, and task attachments. Results are trimmed to the fields a teacher needs.
5. **`browse_lms_folder`**: Walks the folder hierarchy of an enrolled course.
6. **`search_ib_resources`**: Queries the 53,000+ IB resource catalog. Past papers include their paired markscheme.
7. **`select_and_attach_files`**: Autonomously downloads and attaches relevant LMS/IB files to the chat thread for reading.
8. **`read_document`**: Reads decrypted text and checks for diagrams/figures on specified page ranges (`"1-5"`).
9. **`search_within_document`**: Keyword search inside an attached document, returning ranked page numbers and snippets.
10. **`inspect_document_pages`**: Rasterizes PDF pages into PNG images on-demand for vision LLM inspection.
11. **`research_topic`**: Web search and fetch tool for the researcher subagent and fact verification.
12. **`confirm_lms_write`**: Emits an interactive `<WriteConfirmCard>` requiring student confirmation before modifying school data.

### 2.1 Loop guards

The agent takes at most 12 autonomous tool turns per user message. Two guards keep it from stalling:

- **Duplicate call suppression**: a tool call with an identical name and identical arguments is answered once. A repeat returns `{"status": "duplicate"}` and points the model at the earlier result.
- **Prose before cards**: the teacher prompt requires a short lead-in paragraph before every `quiz` call, so the student reads the reason for the check before the card appears.
