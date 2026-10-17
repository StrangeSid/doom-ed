# doom-ed

> **AI Study Coach & Socratic Pedagogy System** grounded in live **Indus LMS** student data, the **53k+ IB Resource Catalog**, and verified **Dependency-Graph Teaching**.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](COPYING)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](requirements.txt)
[![Tests: 107 passed](https://img.shields.io/badge/Tests-107%20passed-brightgreen.svg)](tests/)

---

## 🌟 Key Capabilities

1. **Grounded Student Triage (`/api/study`)**
   - Answers three questions from live Indus LMS data without synthetic/mock fallbacks:
     - **What needs me now?** Overdue work, items due today/tomorrow, today's calendar events, unread notifications, and live attendance status.
     - **What am I weak at?** Automatically extracts wrong questions from graded EOL tests (`score < total`), low rubric criteria on graded Formative/Summative Assessments, and teacher task feedback to construct a real-time **Fix List**.
     - **What should I do before the next break?** Ordered work queue before the next holiday or term break.
2. **Socratic Pedagogy Engine (`app/prompts.py` & `app/orchestrator.py`)**
   - Direct port of the teaching philosophy from `StrangeSid/learning-agent` (`amosblomqvist/learn`):
     - **Principle i — Unconditional truths first**: Builds from caveat-free universal statements and real definitions before introducing nuance.
     - **Principle ii — "How could I have discovered this?"**: Motivate $\to$ Establish $\to$ Connect.
     - **3-Phase Loop**: Phase 1 Probe (calibrated check binary search) $\to$ Phase 2 Plan (dependency graph approval) $\to$ Phase 3 Teach Loop (node-by-node Motivate/Establish/Connect/Check).
3. **Interactive Turn-Gated Cards (`cards.js`)**
   - **`<QuizCard>`**: Multiple choice with options shuffled and index `0: "I don't know"` pinned at bottom. Correct answers are held server-side until the student submits; instant feedback banner (`✓ Correct`, `✗ Incorrect`, `— I don't know` + explanation) gates the next teaching move.
   - **`<AskCard>`**: Single/multi-select options + auto-appended `"Other (type your own)"` free-text input for goal setting and plan approval.
   - **`<WriteConfirmCard>`**: Enforces ask-first confirmation before any Indus LMS write action (`mark_read`, `send_message`, `request_extension`, `submit_task`).
   - **`<VisualCard>`**: Embeds verified Mermaid or SVG diagrams with zoom controls and 1-click export.
4. **Autonomous File Selection & Multimodal Document Reader (`app/file_reader.py`)**
   - Agent can search (`search_lms_files`), browse (`browse_lms_folder`), and attach files (`select_and_attach_files`) autonomously.
   - Reads PDFs, DOCX (with GitHub-flavored Markdown tables), PPTX (slides + speaker notes), and XLSX (worksheets).
   - Rasterizes PDF pages into PNG images on-demand (`inspect_document_pages`) so vision models can inspect circuit diagrams, geometry figures, graphs, and markscheme tables.
5. **IB Resource Catalog Integration (`app/ib_client.py`)**
   - Direct integration with `https://ib-scrape.sidevv.xyz` (53,000+ IB resources).
   - Automatically pairs past papers (`rtype=paper`) with their official markschemes (`rtype=markscheme`) by matching subject, level, year, session, paper number, and timezone.
6. **In-App Slide-Over Viewer (`viewer.js`)**
   - Reads PDFs, DOCX, XLSX, PPTX, images, and Markdown in an in-app drawer without kicking the student out to a raw browser tab.
   - Bidirectional Chat $\leftrightarrow$ Viewer integration: clicking any citation link in chat jumps the viewer to that exact page (`#page=N`); clicking **"Ask about Page N"** in the viewer attaches that page into the chat composer.
7. **Live Obsidian Lesson Log (`log-export.js`)**
   - Real-time lesson log formatted with Obsidian callouts (`> [!question]`, `> [!success]`, `> [!failure]`), inline LaTeX (`$...$`), and diagram embeds (`![[slug.png|550]]`).
   - One-click **Copy Obsidian Markdown** and **Download `.md`** file.
8. **Security & Academic Integrity**
   - **Zero Live Test Leaks**: Server inspects test status before answering or explaining. Any unsubmitted or in-progress EOL test/assessment is rejected with `403 Forbidden` and redirected to syllabus pointers + IB practice bank equivalents.
   - **Encryption at Rest**: Session tokens, L2 cache, private LMS blobs, and extracted page text are encrypted at rest with **Fernet** (`cryptography`).
   - **Per-Owner ACL Enforcement**: Every file request and agent tool call is gated by `(tenant, uid)`.

---

## 🚀 Quick Start

### 1. Requirements & Setup

```bash
# Clone the repository
git clone https://github.com/StrangeSid/doom-ed.git
cd doom-ed

# Create virtual environment and install dependencies
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt -r requirements-dev.txt

# Copy environment template
cp .env.example .env
```

### 2. Configure Environment (`.env`)

```ini
# Secret key for Fernet encryption (auto-generated if omitted)
DOOMED_SECRET_KEY=your-32-byte-secret-passphrase

# OpenAI-compatible API (OpenRouter, Gemini OpenAI endpoint, yetanotherproxy, OpenAI)
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=your-api-key
TEACHER_MODEL=google/gemini-2.5-pro
MAKER_MODEL=google/gemini-2.5-flash
RESEARCHER_MODEL=google/gemini-2.5-flash

# IB Scrape Public Catalog URL
IB_SCRAPE_BASE_URL=https://ib-scrape.sidevv.xyz
```

### 3. Run the Development Server

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open your browser at **`http://127.0.0.1:8000`**.
- If `~/.induslms_token.json` exists on your machine, your session is automatically bootstrapped on startup.
- Otherwise, click **"Sign in with Indus LMS"** and enter your school email and password.

---

## 🧪 Testing

```bash
# Run all unit, integration, and security tests
pytest

# Run Playwright browser end-to-end tests
pytest tests/test_e2e_playwright.py
```

---

## 📂 Repository Layout

```text
doom-ed/
├── app/
│   ├── main.py              # FastAPI application, auth routes, SSE chat, viewer streaming
│   ├── config.py            # Environment settings, secret key derivation, model options
│   ├── sessions.py          # SessionStore in 0600 SQLite with Fernet encryption
│   ├── cache.py             # L1 in-memory singleflight cache + L2 encrypted persistent cache
│   ├── db.py                # Database (threads, messages, FTS5 metadata, file_access ACL, quiz_states)
│   ├── security.py          # Fernet Cipher, SSRF safe_fetch guard, WriteConfirmationManager
│   ├── lms_bridge.py        # Live Indus LMS client, study triage builder, file crawler
│   ├── ib_client.py         # IB catalog search client with paper-markscheme pairing
│   ├── file_reader.py       # Secure blob storage, PyMuPDF + Office extractors, vision rasterizer
│   ├── prompts.py           # Ported system prompts (teach, visualize, mermaid-maker, svg-maker, researcher)
│   ├── orchestrator.py      # Multi-turn SSE loop, card pause/resume, visual maker loop
│   └── research.py          # Web search and fetch tool for researcher subagent
├── web/
│   ├── index.html           # 3-pane responsive interface (Triage | Chat | Viewer & Log)
│   └── assets/
│       ├── style.css        # Academic UI design system, light/dark themes, callout styling
│       ├── app.js           # Client controller, SSE stream parser, triage loader, modals
│       ├── cards.js         # Interactive <QuizCard>, <AskCard>, <WriteConfirmCard>, <VisualCard>
│       ├── viz-engine.js    # Client-side Mermaid + SVG renderer, getBBox() validator, canvas exporter
│       ├── viewer.js        # Slide-over in-app file viewer (PDF.js, pan-zoom, Office preview)
│       ├── renderer.js      # Marked + KaTeX ($/$$) + Obsidian callouts + citation deep-links
│       └── log-export.js    # Live Obsidian lesson log builder, clipboard copy & .md download
├── tests/
│   ├── test_security_cache.py  # Encryption at rest, SSRF guard, per-owner cache isolation
│   ├── test_prompts.py         # Prompt integrity and web search tests
│   ├── test_ib_client.py       # IB paper+markscheme pairing tests
│   ├── test_lms_bridge.py      # Live LMS bridge, triage calculation, write confirmation tests
│   ├── test_file_reader.py     # Multimodal file parsing, FTS search, ACL check, vision rendering
│   ├── test_api.py             # FastAPI endpoint integration tests
│   └── test_e2e_playwright.py  # Real browser Playwright tests for auth, triage, cards, viewer
├── ARCHITECTURE.md          # Deployment & infrastructure architecture
├── DOCS.md                  # Detailed API & tool documentation
├── CONTRIBUTING.md          # Contributing guidelines & development standards
├── CHANGELOG.md             # Version history
└── COPYING                  # GNU General Public License v3.0
```

---

## 📜 License

This project is licensed under the **GNU General Public License v3.0 or later** ([COPYING](COPYING)).
