# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- Studio minimal UI & theme (`web/index.html`, `web/assets/style.css`, `web/assets/home.css`, `web/assets/cards.js`, `web/assets/viewer.js`) with Prussian Ink-Blue primary, honey-ochre momentum cues, `#home-theme-toggle`, `color-scheme` sync, and refreshed `docs/assets/homepage.png`.
- GitHub Actions CI workflow (`.github/workflows/ci.yml`) running Python 3.11 and 3.13 across unit, integration, and Playwright Chromium tests (`tests/browser_helpers.py`, `--tracing=retain-on-failure` via `tests/conftest.py`).
- Isolated temporary test data directory in `tests/conftest.py` so `pytest` never mutates workspace `data/` databases.
- External `/assets/boot.js` script extracting inline theme bootstrap and vendor loading out of `web/index.html`.
- Adaptive practice engine foundation behind `DOOMED_PRACTICE_ENGINE`: skill and item schema, deterministic planner, BKT/Elo mastery updates, FSRS-lite scheduling, owner-scoped attempt ledger, daily feed plans, and the feed/attempt/event/progress/teach API.
- Optional LMS integration plugin layer in `app/integrations/`. The existing Indus bridge is wrapped as the built-in adapter. Local study and the practice engine work with no plugin installed. `GET /api/integrations` reports plugin availability and connection state.
- Practice tests covering grading, scheduling, planner constraints, owner isolation, API flow, migrations, and the plugin boundary.
- Scrollable home feed behind `DOOMED_HOME_FEED`: plan header, reason disclosures, server-graded cards, numeric answers, skip/report, teach handoff, cursor pagination, and batched dwell/impression events.
- Teaching bridge: the teacher prompt receives the practice brief, quiz calls accept `skill_ids`, and lesson answers update the same mastery state and scheduler as feed attempts.
- Browser coverage for the home feed: answering, reload persistence, load more, teach handoff, mobile triage, skip, and report.
- Future work roadmap in `docs/FUTURE_WORK.md`: prioritized content, checkpoint, teaching-loop, UI, operations, and calibration work with milestones and exit criteria.

### Changed
- Restyled the web UI (`web/assets/style.css`, `web/assets/home.css`, `web/index.html`, `web/assets/app.js`) to one neutral system with a single bronze accent: near-black `#111111` / light `#f2f2f2` themes, vendored Host Grotesk + JetBrains Mono (`web/assets/vendor/fonts/`, SIL OFL), one filled primary button per view, 1px hover/press steps and 2px card lifts, no gradients, blurs, glows or translucent tints. Text never drops below 13px; every text colour clears WCAG AA in both themes; controls reach 44px on phones and coarse pointers. Refreshed `docs/assets/homepage.png`.
- Fixed the profile card (`DP 2` no longer wraps; the LMS status pill keeps its own style), made the student name a keyboard-reachable button for the profile dialog, dropped the repeated subject prefix from triage items, removed the inline style on the local-study button, and gave phones a shorter composer placeholder instead of a clipped one.
- Restored correct/incorrect colors on lesson quizzes and secondary styling on card actions. Expanded the vendored fonts to their full upstream character sets, including Latin Extended-A names, replaced unsupported UI symbols with text, CSS, or inline SVG, fixed dark error-toast contrast, and brought feed and icon controls up to 44px touch targets.
- Added `sse-starlette>=2.0.0` to `requirements.txt`, replaced deprecated FastAPI `@app.on_event("startup")` with `lifespan`, imported `LMSUpstreamError`/`LMSNotAuthenticatedError` in `app/file_reader.py`, and removed hardcoded local machine paths from `app/lms_bridge.py` and `scripts/capture_screenshot.py`.
- Login, profile sync, and the teacher's LMS tools resolve an integration plugin instead of calling the bridge directly.
- Thread creation now records `origin` and `origin_json`, so a lesson can carry the practice evidence that opened it.
- `GET /api/session` and the local/LMS sign-in responses expose `features` so the shell can route to the feed without guessing.
- The suite is now 210 tests (200 unit/integration plus 10 browser). All pass.
- Unslop pass across the prose documentation.

### Security
- Standalone `create_local_session` now generates unique per-session `local:local_<hex>` owners instead of sharing `local:student`.
- Hardened SSRF protections: `app/research.py:web_fetch` and `app/ib_client.py` route outbound requests through `safe_fetch`, `_PinnedDNSAdapter` mitigates DNS-rebinding TOCTOU, and `read_bounded_response` + streaming `/api/upload` enforce `MAX_FILE_BYTES` (`413` on oversized uploads).
- Validated `thread_id` against `^[A-Za-z0-9_-]{1,64}$` and enforced `data/viz/` path containment + cross-owner thread non-disclosure.
- Added `assert_not_live_test` checks on FA/SA assessment feedback and learning task detail fetches in `app/lms_bridge.py`; gated `/api/ib/search` and `/api/ib/facets` behind `get_current_session`.
- Added HTTP security headers middleware (`X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, HTTP `Content-Security-Policy` with `frame-ancestors 'none'` and no `'unsafe-inline'` in `script-src`), configurable `DOOMED_COOKIE_SECURE`, sanitized error responses, `safeDownloadUrl` in `viewer.js`, and hardened SVG sanitization (`foreignobject`/`style`/`animate`/`set`/`use`) in `cards.js` and `viz-engine.js`.
- Practice answer keys are Fernet-encrypted at rest and never included in feed responses. Attempt grading is owner-scoped and idempotent.

---

## [0.1.2] - 2026-10-22

### Fixed
- Multi-turn tool execution in `app/orchestrator.py`: assistant message with `tool_calls` is now appended to `llm_messages` before tool results are added, complying with OpenAI API specification and preventing silent tool loop aborts.
- Academic integrity lockout bypass in `app/security.py`: `assert_not_live_test` substring matching on `"graded"` previously matched inside `"ungraded"` and `"not_yet_graded"`. Enforced token boundaries and added explicit blocked status tokens.

### Security
- Strip `Authorization` and `Cookie` headers in `safe_fetch` on cross-origin HTTP 30x redirect hops, preventing credential leakage to third-party domains.

### Changed
- Shrunk test cases across `tests/test_orchestrator.py` and `tests/test_security_hardening.py` to minimal reproducer forms (135 passed tests).
- Unslop pass across documentation and codebase comments.

---

## [0.1.1] - 2026-10-19

### Added
- Rich student profile modal showing school, program, grade, admission number, student ID, and the full enrolled subjects list.
- Collapsible sidebar on desktop and mobile with `localStorage` state persistence.
- Automatic personal session bootstrapping from `~/.induslms_token.json`, opt-in through `DOOMED_AUTO_BOOTSTRAP_TOKEN` and limited to loopback requests.
- Streaming teacher prose: SSE `token` frames reach the browser as the model writes. So the bubble fills while the answer is still being generated.
- `GET /health` liveness endpoint.
- Re-authentication banner. When the Indus token expires, the last known payload stays on screen, the banner names the problem, and a "Sign in again" action opens the sign-in dialog. The banner stays until the student signs in, and the stale copy is only served inside the persistent cache window.
- `tests/test_orchestrator.py` covering quiz state persistence, grading, owner scoping, card round trips, and the visual maker retry loop.
- `tests/test_security_hardening.py` and `tests/test_session_expiry.py` covering the hardening and expiry behaviour.
- `tests/test_frontend_xss.py` proving that LMS and model text can't inject markup into the page.

### Fixed
- `create_quiz_state` was called with an argument the database method doesn't accept, so every quiz card crashed the turn. Grading also read a column name that doesn't exist, so no answer could ever be marked correct.
- The visual maker retry loop referenced an unimported `re` module and crashed on the second iteration.
- Assignment shaping across EOL tests, formative/summative assessments, and learning tasks, so overdue work, due-soon work, and weak-topic fix lists populate with real dates and scores.
- Token expiration detection in `call_lms` now catches API error payloads and triggers a refresh or a re-authentication prompt instead of silently returning empty lists. This was the cause of the empty sidebar and the `502` on `/api/study`.
- The teacher agent no longer burns every turn repeating the same file search. Duplicate tool calls are answered once and the loop budget rose to 12 turns.
- The teacher prompt now requires a lead-in paragraph before a graded check, so a bare card never appears without context.
- Frontend assistant prose is accumulated correctly and reaches the lesson log.
- The SSE frame parser tolerates CRLF line endings and no longer drops frames.
- `purify.min.js` no longer requests a source map that doesn't exist.

### Security
- `ib://<sha>` can no longer hand out a cached private school file. Only blobs recorded as public IB resources are reused through that namespace, and ref lookups are owner-scoped.
- Served files get their MIME type sniffed from the bytes. Only PDFs, images, and plain text are served inline; anything else is an attachment. Every file response carries `nosniff` and a sandboxing CSP.
- The visual slug is sanitised and the write target is verified to stay inside the thread's visual directory. PNG magic bytes and a size cap are checked. A visual can no longer be published into another student's thread.
- `/api/settings` reports `api_key_set` instead of the key. Model routing changes require a loopback request.
- A card reply can no longer write into another student's thread.
- Indus LMS and model text is escaped before it reaches the DOM, the sanitizer fails closed when it is missing, and the page runs under a CSP.
- School file content is wrapped and labelled as untrusted, and the teacher prompt states that file content is data, never instruction.
- A malformed `Range` header returns `416` instead of crashing the request.

### Removed
- Attendance component removed from the backend payload, triage calculation, and frontend UI.
- Client-side LLM provider input removed. Model routing is managed on the backend through `config.json`.

---

## [0.1.0] - 2026-10-17

### Added
- **Live Indus LMS Bridge (`app/lms_bridge.py`)**:
  - Live payload fetch (courses, EOL tests, FA/SA assessments, learning tasks, calendar, attendance, notices).
  - Background course resource crawler and FTS5 metadata indexing scoped to student `owner`.
  - Grounded 3-question Study Coach triage (`/api/study`): "What needs me now?", "What am I weak at?" (fix list with wrong questions), and "What should I do before the next break?".
  - Safety rails: `assert_not_live_test` blocking unsubmitted/live test leaks, and single-use write confirmation tokens (`WriteConfirmationManager`).
- **53k+ IB Resource Catalog Client (`app/ib_client.py`)**:
  - Full-text search over past papers, markschemes, questionbanks, and notes.
  - Automatic paper $\leftrightarrow$ markscheme pairing by matching subject, level, year, session, paper number, and timezone.
- **Multimodal Secure File Reader (`app/file_reader.py`)**:
  - Fernet encryption at rest for private LMS and uploaded files (`blobs/private/`).
  - PyMuPDF, `python-docx` (with Markdown tables), `python-pptx` (slides + notes), and `openpyxl` extractors.
  - On-demand PDF page $\to$ PNG rasterization (`inspect_document_pages`) for vision LLM inspection.
  - Mandatory per-owner ACL gate (`file_access` table).
- **Pedagogy Engine & Visual Maker Loop (`app/prompts.py` + `app/orchestrator.py`)**:
  - Port of `learning-agent` dependency-graph pedagogy: Principle i (Unconditional truths first) and Principle ii ("How could I have discovered this?").
  - 3-Phase Socratic loop: Probe $\to$ Plan $\to$ Teach.
  - Web-native visual maker loop for Mermaid and SVG diagrams with browser DOM geometry inspection and canvas PNG feedback.
- **Vanilla Zero-Build Web Interface (`web/`)**:
  - 3-pane responsive academic UI with light/dark theme variables (`web/index.html`, `web/assets/style.css`).
  - Interactive turn-gated cards (`web/assets/cards.js`): `<QuizCard>` (with pinned index 0 "I don't know"), `<AskCard>`, `<WriteConfirmCard>`, `<VisualCard>`.
  - In-app slide-over file viewer (`web/assets/viewer.js`) supporting PDF.js page streaming, pan/zoom, and bidirectional chat citations.
  - Live Obsidian lesson log drawer (`web/assets/log-export.js`) with 1-click Markdown copy and download.
- **Automated Test Suite (`tests/`)**:
  - 107 tests passing across unit, security, integration, and real-browser Playwright tests.
- **Deployment Blueprint (`ARCHITECTURE.md`)**:
  - Google Cloud Run container deployment with Litestream SQLite replication to GCS, Cloudflare proxy, and Vercel serverless sidecar.
