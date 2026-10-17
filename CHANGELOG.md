# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
