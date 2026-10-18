# doom-ed

AI study coach grounded in live Indus LMS student data, the 53k+ IB resource index, and Socratic dependency-graph pedagogy.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](COPYING)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](requirements.txt)
[![Tests: 107 passed](https://img.shields.io/badge/Tests-107%20passed-brightgreen.svg)](tests/)

---

## Core capabilities

1. **Student triage (`/api/study`)**
   - Answers three questions from live LMS data with zero synthetic mocks:
     - **What needs me now?** Overdue tasks, work due today/tomorrow, today's timetable events, unread notifications, and live attendance.
     - **What am I weak at?** Extracts wrong questions from graded EOL tests (`score < total`), low rubric criteria on graded assessments, and teacher comments into an actionable fix list.
     - **What should I do before the next break?** Ordered queue of upcoming work before the next school break.
2. **Teaching engine (`app/prompts.py`, `app/orchestrator.py`)**
   - Port of `StrangeSid/learning-agent` (`amosblomqvist/learn`) pedagogy:
     - **Principle i — Unconditional truths first**: Anchor understanding in caveat-free universal statements and core definitions before adding edge cases.
     - **Principle ii — "How could I have discovered this?"**: Motivate $\to$ Establish $\to$ Connect.
     - **3-Phase loop**: Phase 1 Probe (calibrated check binary search) $\to$ Phase 2 Plan (dependency map review) $\to$ Phase 3 Teach loop (node-by-node Motivate/Establish/Connect/Check).
3. **Turn-gated prompt cards (`web/assets/cards.js`)**
   - **`<QuizCard>`**: Shuffled choices with index `0: "I don't know"` pinned at the bottom. Correct answers stay server-side until submission. Instant feedback (`✓ Correct`, `✗ Incorrect`, `— I don't know` + explanation) gates the next teaching move.
   - **`<AskCard>`**: Single/multi-choice with an inline "Other (type your own)" text field for goal setting and plan approval.
   - **`<WriteConfirmCard>`**: Enforces explicit student confirmation before executing any school LMS write (`mark_read`, `send_message`, `request_extension`, `submit_task`).
   - **`<VisualCard>`**: Embeds verified Mermaid or SVG diagrams with zoom controls and SVG/PNG export.
4. **Autonomous file access and document reading (`app/file_reader.py`)**
   - The assistant searches, browses, and attaches course files and IB past papers on its own.
   - Reads PDFs, DOCX (with table parsing), PPTX (slides + speaker notes), and XLSX spreadsheets.
   - Rasterizes PDF pages to PNG on demand (`inspect_document_pages`) so vision models can read circuit diagrams, geometry figures, graphs, and markscheme tables.
5. **IB resource catalog integration (`app/ib_client.py`)**
   - Direct integration with `https://ib-scrape.sidevv.xyz` (53,000+ IB papers, markschemes, questionbanks, and notes).
   - Automatically pairs question papers with matching markschemes by subject, level, session, year, paper number, and timezone.
6. **In-app slide-over viewer (`web/assets/viewer.js`)**
   - Opens PDFs, DOCX, XLSX, PPTX, images, and Markdown in a side drawer without kicking out to raw browser tabs.
   - Clicking citation links jumps to the exact page (`#page=N`). Clicking **"Ask about Page N"** sends the page citation directly into the chat composer.
7. **Obsidian lesson log (`web/assets/log-export.js`)**
   - Real-time lesson log formatted with Obsidian callouts (`> [!question]`, `> [!success]`, `> [!failure]`), inline math (`$...$`), and diagram embeds (`![[slug.png|550]]`).
   - One-click clipboard copy and markdown download.
8. **Security and academic integrity**
   - **No live test leaks**: The server checks test status before answering. Unsubmitted or in-progress tests return `403 Forbidden` and route to syllabus pointers and practice papers instead.
   - **Encryption at rest**: Session tokens, L2 cache, private school blobs, and extracted text are encrypted with Fernet.
   - **Tenant isolation**: Every file request and agent tool call is gated by student ownership `(tenant, uid)`.

---

## Quick start

### 1. Requirements and setup

```bash
git clone https://github.com/StrangeSid/doom-ed.git
cd doom-ed

uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env
cp config.example.json config.json
```

### 2. Configure environment and models

Edit `config.json` (or `.env`):

```json
{
  "llm": {
    "base_url": "https://openrouter.ai/api/v1",
    "api_key": "your-key",
    "teacher_model": "google/gemini-2.5-pro",
    "maker_model": "google/gemini-2.5-flash",
    "researcher_model": "google/gemini-2.5-flash"
  }
}
```

### 3. Run the development server

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000`.
- If `~/.induslms_token.json` exists on your system, the session bootstraps automatically on launch.
- Otherwise, click **"Sign in with Indus LMS"** and enter your school credentials.

---

## Testing

```bash
# Run backend, security, and unit tests
pytest

# Run Playwright browser tests
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
