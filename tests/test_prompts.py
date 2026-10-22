"""tests/test_prompts.py - Unit tests for prompts, pedagogy system, and research tools.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from app.prompts import (
    TEACH_SYSTEM_PROMPT,
    VISUALIZE_SKILL_PROMPT,
    MERMAID_MAKER_PROMPT,
    SVG_MAKER_PROMPT,
    RESEARCHER_PROMPT,
    build_teacher_system_prompt,
)
from app.research import (
    strip_html_content,
    web_fetch,
    web_search,
    _search_ddg_html,
    _search_wikipedia_api,
    _search_arxiv_api,
)


def _contains(doc: str, required) -> None:
    """Assert each string (or tuple of alternative strings) appears in doc."""
    for item in required:
        alternatives = (item,) if isinstance(item, str) else item
        assert any(alt in doc for alt in alternatives), f"missing: {item}"


def _resp(text="", *, status=200, ctype="text/html; charset=utf-8", payload=None, content=None):
    resp = MagicMock(status_code=status, headers={"Content-Type": ctype}, text=text)
    resp.json.return_value = payload
    if content is not None:
        resp.content = content
    return resp


# ---------------------------------------------------------------------------
# Prompt Constants Verification
# ---------------------------------------------------------------------------

def test_prompts_defined_and_non_empty():
    """All required prompt constants must be defined non-empty strings."""
    for prompt in (TEACH_SYSTEM_PROMPT, VISUALIZE_SKILL_PROMPT, MERMAID_MAKER_PROMPT,
                   SVG_MAKER_PROMPT, RESEARCHER_PROMPT):
        assert isinstance(prompt, str) and len(prompt.strip()) > 200


def test_teach_system_prompt_principles():
    """TEACH_SYSTEM_PROMPT must preserve every core pedagogical principle word-for-word."""
    _contains(TEACH_SYSTEM_PROMPT, [
        "Connected knowledge > disconnected knowledge", "A graph of dependencies > disjoint lonely nodes",
        "Understanding > memorizing", "the click",
        "Principle i — Unconditional truths first", "unconditional truth", "axiom",
        "Universal statements", "Real definitions", "Confirm the foundation before building on it",
        'Principle ii — "How could I have discovered this?"',
        ("reconstruct the *necessity*", "reconstruct the necessity"), "Narrative", "Socratic",
        "Phase 1 — Probe", "Never skip probe", "Phase 2 — Plan", "Phase 3 — The teach loop",
        "Motivate", "Establish", "Connect", "Quiz-check",
        "Writing Question / Quiz Options — Construction Procedure", "Every option is a bare claim",
        "Derive distractors by mutating the correct answer", "Never advance past a missed check",
        "search_lms_files", "browse_lms_folder", "select_and_attach_files",
        "search_ib_resources", "read_document", "inspect_document_pages",
        "Never answer or submit live/unsubmitted tests or assessments",
        "LMS writes always require ask-first confirmation",
    ])


def test_visualize_skill_prompt():
    """VISUALIZE_SKILL_PROMPT must contain rules for when to visualize and brief pruning."""
    _contains(VISUALIZE_SKILL_PROMPT, [
        "A picture earns its place only when it shows something words can't",
        "creative director",
        "Do NOT visualize when prose or a single equation already carries it",
        'kind="mermaid"',
        'kind="svg"',
        "Brief well: one idea, fewest elements",
        "if I delete this, is the idea still clear?",
        ("5–7 elements", "5-7 elements"),
    ])


def test_mermaid_maker_prompt():
    """MERMAID_MAKER_PROMPT must include authoring tools, visual inspection, and output format."""
    _contains(MERMAID_MAKER_PROMPT, [
        "write_mermaid", "edit_mermaid", "render_mermaid", "verify by looking",
        "DOM layout warnings", "RESULT:", "save_as", "7 nodes",
    ])


def test_svg_maker_prompt():
    """SVG_MAKER_PROMPT must include exact control, authoring tools, bounding-box checks."""
    _contains(SVG_MAKER_PROMPT, [
        "write_svg", "edit_svg", "render_svg", "verify by looking", "viewBox",
        ("bounding-box", "bounding box"), "RESULT:",
    ])


def test_researcher_prompt():
    """RESEARCHER_PROMPT must define 2-4 facets, tool usage, and structured deliverable format."""
    _contains(RESEARCHER_PROMPT, [
        "2-4 searchable facets", "web_search", "web_fetch", "search_ib_resources",
        "## Summary", "## Findings", "## Sources", "## Gaps",
    ])


# ---------------------------------------------------------------------------
# build_teacher_system_prompt Verification
# ---------------------------------------------------------------------------

def test_build_teacher_system_prompt_defaults():
    """build_teacher_system_prompt defaults should compose TEACH, VISUALIZE, and default states."""
    _contains(build_teacher_system_prompt(), [
        TEACH_SYSTEM_PROMPT.strip(), VISUALIZE_SKILL_PROMPT.strip(), "PHASE 1 — PROBE",
        "No student LMS profile", "No files are currently attached",
    ])


def test_build_teacher_system_prompt_phase_variations():
    """build_teacher_system_prompt should adjust instructions based on thread_phase."""
    _contains(build_teacher_system_prompt(thread_phase="probe"),
              ["PHASE 1 — PROBE", "begin teaching explanations"])
    _contains(build_teacher_system_prompt(thread_phase="plan"),
              ["PHASE 2 — PLAN", "start teaching the individual nodes"])
    _contains(build_teacher_system_prompt(thread_phase="teach"),
              ["PHASE 3 — TEACH LOOP", "NEVER advance past a missed check"])


def test_build_teacher_system_prompt_with_context_and_files():
    """build_teacher_system_prompt should format student context and attached files cleanly."""
    student_ctx = {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "student_id": "ST12345",
        "courses": [
            {"title": "IB Mathematics AA HL", "code": "MATH-HL", "id": 101},
            {"title": "IB Physics HL", "code": "PHYS-HL", "id": 102},
        ],
        "triage": [
            {"title": "Differential Calculus EOL", "due": "2026-10-25", "status": "graded"},
            {"title": "Wave Optics FA", "due": "2026-10-30", "status": "pending"},
        ],
    }
    attached = [
        {"filename": "Calculus_Syllabus.pdf", "ref": "lms://resource/101/1", "page_count": 14, "mime": "application/pdf"},
        {"filename": "IB_Math_Formula_Booklet.pdf", "ref": "ib://abcd1234ef", "page_count": 40, "mime": "application/pdf"},
    ]
    prompt = build_teacher_system_prompt(
        student_context=student_ctx, thread_phase="plan", attached_files=attached
    )
    _contains(prompt, [
        "Jane Doe", "jane@example.com", "IB Mathematics AA HL", "code: MATH-HL",
        "Differential Calculus EOL", "Calculus_Syllabus.pdf", "IB_Math_Formula_Booklet.pdf",
        "lms://resource/101/1", "Attached Lesson Files (2 files)",
    ])


# ---------------------------------------------------------------------------
# Research Tools Verification
# ---------------------------------------------------------------------------

def test_strip_html_content():
    """strip_html_content should remove scripts/styles/comments, extract title, and clean text."""
    sample_html = """
    <html><head><title>Photosynthesis and Cellular Respiration</title>
    <style>body { font-size: 16px; color: #333; }</style>
    <script>function tracker() { return "bad_secret_code"; }</script></head>
    <body><!-- Hidden developer comment -->
    <h1>Understanding Cellular Energy</h1>
    <p>ATP is the primary energy carrier in cells &amp; tissues.</p>
    <ul><li>Glycolysis occurs in cytoplasm.</li><li>Krebs cycle occurs in mitochondria.</li></ul>
    <script type="text/javascript">alert("another script to strip");</script>
    <p>Final summary paragraph.</p></body></html>
    """
    title, text = strip_html_content(sample_html)

    assert title == "Photosynthesis and Cellular Respiration"
    for junk in ("bad_secret_code", "another script to strip",
                 "Hidden developer comment", "font-size: 16px"):
        assert junk not in text
    for kept in ("Understanding Cellular Energy",
                 "ATP is the primary energy carrier in cells & tissues.",
                 "Glycolysis occurs in cytoplasm.",
                 "Krebs cycle occurs in mitochondria.",
                 "Final summary paragraph."):
        assert kept in text


def test_strip_html_content_empty():
    """strip_html_content on empty string returns empty strings."""
    assert strip_html_content("") == ("", "")
    assert strip_html_content(None) == ("", "")


def test_web_fetch_invalid_url():
    """web_fetch should return error on invalid schemes."""
    for url in ("ftp://example.com", "javascript:void(0)", ""):
        res = web_fetch(url)
        assert res["status"] == 0
        assert "Invalid URL scheme" in res["error"]


def test_web_fetch_success_mock():
    """web_fetch should extract title and readable text from HTTP 200 response."""
    html = ("<html><head><title>Mocked Title</title><script>var x = 1;</script></head>"
            "<body><p>Hello World Content</p></body></html>")
    with patch("requests.get", return_value=_resp(html)):
        res = web_fetch("https://example.com/test", max_chars=100)
    assert res["url"] == "https://example.com/test"
    assert res["title"] == "Mocked Title"
    assert "Hello World Content" in res["text"]
    assert "var x = 1" not in res["text"]
    assert res["status"] == 200
    assert res["error"] is None


def test_web_fetch_truncation():
    """web_fetch should truncate text to max_chars."""
    with patch("requests.get", return_value=_resp("A" * 500, ctype="text/plain")):
        res = web_fetch("https://example.com/long", max_chars=50)
    assert res["text"] == "A" * 50
    assert len(res["text"]) == 50


def test_web_fetch_http_error():
    """web_fetch should handle HTTP 404/500 errors gracefully."""
    with patch("requests.get", return_value=_resp("<html><body>Not Found</body></html>", status=404)):
        res = web_fetch("https://example.com/missing")
    assert res["status"] == 404
    assert res["error"] == "HTTP 404"
    assert res["text"] == ""


def test_web_fetch_network_exception():
    """web_fetch should handle network timeouts and connection errors."""
    with patch("requests.get", side_effect=requests.exceptions.Timeout("Connection timed out")):
        res = web_fetch("https://example.com/timeout")
    assert res["status"] == 0
    assert "timed out" in res["error"]
    assert res["text"] == ""


def test_web_search_empty_query():
    """web_search with empty or whitespace query returns empty list."""
    assert web_search("") == []
    assert web_search("   ") == []


def test_search_ddg_html_parsing():
    """_search_ddg_html should extract title, direct URL, and snippet from DDG HTML."""
    sample_ddg_html = """
    <div class="result results_links web-result">
        <a class="result__a" href="/l/?kh=-1&uddg=https%3A%2F%2Fwww.khanacademy.org%2Fmath%2Fcalculus">Introduction to Integral Calculus</a>
        <a class="result__snippet">Learn integral calculus with step by step examples and practice problems.</a>
    </div>
    <div class="result results_links web-result">
        <a class="result__a" href="https://en.wikipedia.org/wiki/Integral">Integral - Wikipedia</a>
        <a class="result__snippet">In mathematics, an integral assigns numbers to functions in a way that describes displacement.</a>
    </div>
    """
    with patch("requests.post", return_value=_resp(sample_ddg_html)):
        results = _search_ddg_html("integral calculus", max_results=5)
    assert len(results) == 2
    assert results[0]["title"] == "Introduction to Integral Calculus"
    assert results[0]["url"] == "https://www.khanacademy.org/math/calculus"
    assert "step by step" in results[0]["snippet"]
    assert results[1]["url"] == "https://en.wikipedia.org/wiki/Integral"


def test_search_wikipedia_api():
    """_search_wikipedia_api should parse Wikipedia OpenSearch API results."""
    payload = {"query": {"search": [{
        "title": "Quantum mechanics",
        "snippet": 'Central branch of <span class="searchmatch">physics</span> dealing with atomic scales.',
    }]}}
    with patch("requests.get", return_value=_resp(payload=payload)):
        results = _search_wikipedia_api("quantum mechanics", max_results=5)
    assert len(results) == 1
    assert results[0]["title"] == "Quantum mechanics"
    assert results[0]["url"] == "https://en.wikipedia.org/wiki/Quantum_mechanics"
    assert "<span" not in results[0]["snippet"]
    assert "Central branch" in results[0]["snippet"]


def test_search_arxiv_api():
    """_search_arxiv_api should parse arXiv Atom XML results."""
    sample_xml = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <id>http://arxiv.org/abs/2101.00001v1</id>
        <title>Attention Is All You Need</title>
        <summary>The dominant sequence transduction models are based on complex recurrent or convolutional neural networks.</summary>
    </entry></feed>
    """
    with patch("requests.get", return_value=_resp(content=sample_xml.encode("utf-8"))):
        results = _search_arxiv_api("transformer", max_results=5)
    assert len(results) == 1
    assert results[0]["title"] == "Attention Is All You Need"
    assert results[0]["url"] == "http://arxiv.org/abs/2101.00001v1"
    assert "dominant sequence" in results[0]["snippet"]


def test_web_search_fallback_cascade():
    """web_search falls back to Wikipedia when DDG fails, and arXiv when Wikipedia fails."""
    wiki = [{"title": "Wiki Result", "url": "https://wiki.org", "snippet": "Snip"}]
    arxiv = [{"title": "Arxiv Paper", "url": "http://arxiv.org", "snippet": "Abstract"}]
    # DDG fails, Wikipedia succeeds
    with patch("app.research._search_ddg_html", return_value=[]), \
         patch("app.research._search_wikipedia_api", return_value=wiki):
        assert [r["title"] for r in web_search("test query")] == ["Wiki Result"]
    # DDG and Wikipedia fail, arXiv succeeds
    with patch("app.research._search_ddg_html", return_value=[]), \
         patch("app.research._search_wikipedia_api", return_value=[]), \
         patch("app.research._search_arxiv_api", return_value=arxiv):
        assert [r["title"] for r in web_search("test query")] == ["Arxiv Paper"]
    # All raise exceptions
    with patch("app.research._search_ddg_html", side_effect=Exception("DDG error")), \
         patch("app.research._search_wikipedia_api", side_effect=Exception("Wiki error")), \
         patch("app.research._search_arxiv_api", side_effect=Exception("arXiv error")):
        assert web_search("test query") == []
