# DOCS · doom-ed API & Tool Reference

Complete API and agent tool specifications for **doom-ed**.

---

## 1. HTTP API Endpoints

### 1.1 Authentication & Sessions

| Endpoint | Method | Body / Params | Description |
|---|---|---|---|
| `/api/login` | `POST` | `{"email": "...", "password": "..."}` | Authenticates against Indus LMS, establishes session, and returns HttpOnly `doomed_sid` cookie. |
| `/api/logout` | `POST` | — | Clears server-side session and deletes cookie. |
| `/api/session` | `GET` | — | Returns authenticated student profile (`name`, `email`, `uid`, `tenant`, `grade`). Returns `401` if not authenticated. |

---

### 1.2 Study Coach Triage

| Endpoint | Method | Body / Params | Description |
|---|---|---|---|
| `/api/study` | `GET` | `refresh=true` (optional) | Returns 3-question student triage: `now` (overdue, due soon, calendar today, unread notices, attendance), `weak_topics` (fix list with scores & wrong questions), and `before_break` (upcoming load queue). |

---

### 1.3 Chat & Pedagogy Sessions

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

### 1.4 Files, Viewer & Blobs

| Endpoint | Method | Body / Params | Description |
|---|---|---|---|
| `/api/files/{sha256}` | `GET` | Supports HTTP `Range: bytes=...` | Serves decrypted file bytes with `Accept-Ranges`, `Content-Length`, and `Content-Type` for the in-app slide-over viewer (`viewer.js`). Gated by strict per-owner ACL (`can_access_file`). |
| `/api/upload` | `POST` | `multipart/form-data` with `file` | Uploads student file, Fernet-encrypts it on disk, indexes text/pages, and attaches to active thread. |
| `/api/viz/{visual_id}/png` | `GET` | — | Serves rasterized PNG of published visual diagram. |

---

### 1.5 IB Resource Catalog Proxy

| Endpoint | Method | Body / Params | Description |
|---|---|---|---|
| `/api/ib/search` | `GET` | `q`, `rtype`, `level`, `year`, `session`, `limit` | Ranked full-text search across 53k+ IB resources with automatic paper $\leftrightarrow$ markscheme pairing. |
| `/api/ib/facets` | `GET` | `q`, `kind` | Resource aggregation counts. |
| `/api/ib/resources/{sha256}` | `GET` | — | Metadata record for specific IB resource. |

---

### 1.6 Runtime Settings

| Endpoint | Method | Body / Params | Description |
|---|---|---|---|
| `/api/settings` | `GET` | — | Returns current LLM configuration (`base_url`, `teacher_model`, `maker_model`, `researcher_model`). |
| `/api/settings` | `POST` | `{"base_url": "...", "api_key": "...", "teacher_model": "..."}` | Updates runtime model configuration. |

---

## 2. Agent Tool Reference

The teacher agent has 12 specialized tools:

1. **`quiz`**: Poses ONE calibrated multiple-choice check to bracket knowledge or test node understanding. Options are client-shuffled; correct answer and explanation are held server-side until student submission.
2. **`ask_user_question`**: Poses an ungraded question for goal-setting (Phase 1 Part B) or dependency-graph approval (Phase 2).
3. **`create_visual`**: Dispatches `mermaid-maker` or `svg-maker` with a pruned brief ($\le 5\text{–}7$ elements) for web-native visual authoring.
4. **`search_lms_files`**: FTS5 search across student's enrolled course materials, notes, slides, and task attachments.
5. **`browse_lms_folder`**: Walks folder hierarchy of an enrolled course.
6. **`search_ib_resources`**: Queries the 53,000+ IB resource catalog. Past papers include their paired markscheme.
7. **`select_and_attach_files`**: Autonomously downloads and attaches relevant LMS/IB files to the chat thread for reading.
8. **`read_document`**: Reads decrypted text and checks for diagrams/figures on specified page ranges (`"1-5"`).
9. **`search_within_document`**: Keyword search inside an attached document, returning ranked page numbers and snippets.
10. **`inspect_document_pages`**: Rasterizes PDF pages into PNG images on-demand for vision LLM inspection.
11. **`research_topic`**: Web search and fetch tool for researcher subagent and fact verification.
12. **`confirm_lms_write`**: Emits an interactive `<WriteConfirmCard>` requiring student confirmation before modifying school data.
