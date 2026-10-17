"""tests/test_prompts.py - Unit tests for prompts, pedagogy system, and research tools.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
import pytest

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


# ---------------------------------------------------------------------------
# Prompt Constants Verification
# ---------------------------------------------------------------------------

def test_prompts_defined_and_non_empty():
    """All required prompt constants must be defined non-empty strings."""
    for prompt, name in [
        (TEACH_SYSTEM_PROMPT, "TEACH_SYSTEM_PROMPT"),
        (VISUALIZE_SKILL_PROMPT, "VISUALIZE_SKILL_PROMPT"),
        (MERMAID_MAKER_PROMPT, "MERMAID_MAKER_PROMPT"),
        (SVG_MAKER_PROMPT, "SVG_MAKER_PROMPT"),
        (RESEARCHER_PROMPT, "RESEARCHER_PROMPT"),
    ]:
        assert isinstance(prompt, str), f"{name} must be a str"
        assert len(prompt.strip()) > 200, f"{name} is too short ({len(prompt)} chars)"


def test_teach_system_prompt_principles():
    """TEACH_SYSTEM_PROMPT must preserve every core pedagogical principle word-for-word."""
    # Core philosophy
    assert "Connected knowledge > disconnected knowledge" in TEACH_SYSTEM_PROMPT
    assert "A graph of dependencies > disjoint lonely nodes" in TEACH_SYSTEM_PROMPT
    assert "Understanding > memorizing" in TEACH_SYSTEM_PROMPT
    assert "the click" in TEACH_SYSTEM_PROMPT

    # Principle i
    assert "Principle i — Unconditional truths first" in TEACH_SYSTEM_PROMPT
    assert "unconditional truth" in TEACH_SYSTEM_PROMPT
    assert "axiom" in TEACH_SYSTEM_PROMPT
    assert "Universal statements" in TEACH_SYSTEM_PROMPT
    assert "Real definitions" in TEACH_SYSTEM_PROMPT
    assert "Confirm the foundation before building on it" in TEACH_SYSTEM_PROMPT

    # Principle ii
    assert 'Principle ii — "How could I have discovered this?"' in TEACH_SYSTEM_PROMPT
    assert "reconstruct the *necessity*" in TEACH_SYSTEM_PROMPT or "reconstruct the necessity" in TEACH_SYSTEM_PROMPT
    assert "Narrative" in TEACH_SYSTEM_PROMPT
    assert "Socratic" in TEACH_SYSTEM_PROMPT

    # 3-Phase Flow
    assert "Phase 1 — Probe" in TEACH_SYSTEM_PROMPT
    assert "Never skip probe" in TEACH_SYSTEM_PROMPT
    assert "Phase 2 — Plan" in TEACH_SYSTEM_PROMPT
    assert "Phase 3 — The teach loop" in TEACH_SYSTEM_PROMPT
    assert "Motivate" in TEACH_SYSTEM_PROMPT
    assert "Establish" in TEACH_SYSTEM_PROMPT
    assert "Connect" in TEACH_SYSTEM_PROMPT
    assert "Quiz-check" in TEACH_SYSTEM_PROMPT

    # Graded check & quiz construction procedure
    assert "Writing Question / Quiz Options — Construction Procedure" in TEACH_SYSTEM_PROMPT
    assert "Every option is a bare claim" in TEACH_SYSTEM_PROMPT
    assert "Derive distractors by mutating the correct answer" in TEACH_SYSTEM_PROMPT
    assert "Never advance past a missed check" in TEACH_SYSTEM_PROMPT

    # Grounding in LMS and IB
    assert "search_lms_files" in TEACH_SYSTEM_PROMPT
    assert "browse_lms_folder" in TEACH_SYSTEM_PROMPT
    assert "select_and_attach_files" in TEACH_SYSTEM_PROMPT
    assert "search_ib_resources" in TEACH_SYSTEM_PROMPT
    assert "read_document" in TEACH_SYSTEM_PROMPT
    assert "inspect_document_pages" in TEACH_SYSTEM_PROMPT

    # Safety rules
    assert "Never answer or submit live/unsubmitted tests or assessments" in TEACH_SYSTEM_PROMPT
    assert "LMS writes always require ask-first confirmation" in TEACH_SYSTEM_PROMPT


def test_visualize_skill_prompt():
    """VISUALIZE_SKILL_PROMPT must contain rules for when to visualize and brief pruning."""
    assert "A picture earns its place only when it shows something words can't" in VISUALIZE_SKILL_PROMPT
    assert "creative director" in VISUALIZE_SKILL_PROMPT
    assert "Do NOT visualize when prose or a single equation already carries it" in VISUALIZE_SKILL_PROMPT
    assert "kind=\"mermaid\"" in VISUALIZE_SKILL_PROMPT
    assert "kind=\"svg\"" in VISUALIZE_SKILL_PROMPT
    assert "Brief well: one idea, fewest elements" in VISUALIZE_SKILL_PROMPT
    assert "if I delete this, is the idea still clear?" in VISUALIZE_SKILL_PROMPT
    assert "5–7 elements" in VISUALIZE_SKILL_PROMPT or "5-7 elements" in VISUALIZE_SKILL_PROMPT


def test_mermaid_maker_prompt():
    """MERMAID_MAKER_PROMPT must include authoring tools, visual inspection, and output format."""
    assert "write_mermaid" in MERMAID_MAKER_PROMPT
    assert "edit_mermaid" in MERMAID_MAKER_PROMPT
    assert "render_mermaid" in MERMAID_MAKER_PROMPT
    assert "verify by looking" in MERMAID_MAKER_PROMPT
    assert "DOM layout warnings" in MERMAID_MAKER_PROMPT
    assert "RESULT:" in MERMAID_MAKER_PROMPT
    assert "save_as" in MERMAID_MAKER_PROMPT
    assert "7 nodes" in MERMAID_MAKER_PROMPT


def test_svg_maker_prompt():
    """SVG_MAKER_PROMPT must include exact control, authoring tools, bounding-box checks."""
    assert "write_svg" in SVG_MAKER_PROMPT
    assert "edit_svg" in SVG_MAKER_PROMPT
    assert "render_svg" in SVG_MAKER_PROMPT
    assert "verify by looking" in SVG_MAKER_PROMPT
    assert "viewBox" in SVG_MAKER_PROMPT
    assert "bounding-box" in SVG_MAKER_PROMPT or "bounding box" in SVG_MAKER_PROMPT
    assert "RESULT:" in SVG_MAKER_PROMPT


def test_researcher_prompt():
    """RESEARCHER_PROMPT must define 2-4 facets, tool usage, and structured deliverable format."""
    assert "2-4 searchable facets" in RESEARCHER_PROMPT
    assert "web_search" in RESEARCHER_PROMPT
    assert "web_fetch" in RESEARCHER_PROMPT
    assert "search_ib_resources" in RESEARCHER_PROMPT
    assert "## Summary" in RESEARCHER_PROMPT
    assert "## Findings" in RESEARCHER_PROMPT
    assert "## Sources" in RESEARCHER_PROMPT
    assert "## Gaps" in RESEARCHER_PROMPT


# ---------------------------------------------------------------------------
# build_teacher_system_prompt Verification
# ---------------------------------------------------------------------------

def test_build_teacher_system_prompt_defaults():
    """build_teacher_system_prompt defaults should compose TEACH, VISUALIZE, and default states."""
    prompt = build_teacher_system_prompt()
    assert TEACH_SYSTEM_PROMPT.strip() in prompt
    assert VISUALIZE_SKILL_PROMPT.strip() in prompt
    assert "PHASE 1 — PROBE" in prompt
    assert "No student LMS profile" in prompt
    assert "No files are currently attached" in prompt


def test_build_teacher_system_prompt_phase_variations():
    """build_teacher_system_prompt should adjust instructions based on thread_phase."""
    probe_prompt = build_teacher_system_prompt(thread_phase="probe")
    assert "PHASE 1 — PROBE" in probe_prompt
    assert "begin teaching explanations" in probe_prompt

    plan_prompt = build_teacher_system_prompt(thread_phase="plan")
    assert "PHASE 2 — PLAN" in plan_prompt
    assert "start teaching the individual nodes" in plan_prompt

    teach_prompt = build_teacher_system_prompt(thread_phase="teach")
    assert "PHASE 3 — TEACH LOOP" in teach_prompt
    assert "NEVER advance past a missed check" in teach_prompt


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
        student_context=student_ctx,
        thread_phase="plan",
        attached_files=attached,
    )

    assert "Jane Doe" in prompt
    assert "jane@example.com" in prompt
    assert "IB Mathematics AA HL" in prompt
    assert "code: MATH-HL" in prompt
    assert "Differential Calculus EOL" in prompt
    assert "Calculus_Syllabus.pdf" in prompt
    assert "IB_Math_Formula_Booklet.pdf" in prompt
    assert "lms://resource/101/1" in prompt
    assert "Attached Lesson Files (2 files)" in prompt


# ---------------------------------------------------------------------------
# Research Tools Verification
# ---------------------------------------------------------------------------

def test_strip_html_content():
    """strip_html_content should remove scripts/styles/comments, extract title, and clean text."""
    sample_html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Photosynthesis and Cellular Respiration</title>
        <style>
            body { font-size: 16px; color: #333; }
        </style>
        <script>
            function tracker() { return "bad_secret_code"; }
        </script>
    </head>
    <body>
        <!-- Hidden developer comment -->
        <h1>Understanding Cellular Energy</h1>
        <p>ATP is the primary energy carrier in cells &amp; tissues.</p>
        <div>
            <ul>
                <li>Glycolysis occurs in cytoplasm.</li>
                <li>Krebs cycle occurs in mitochondria.</li>
            </ul>
        </div>
        <script type="text/javascript">
            alert("another script to strip");
        </script>
        <p>Final summary paragraph.</p>
    </body>
    </html>
    """
    title, text = strip_html_content(sample_html)

    assert title == "Photosynthesis and Cellular Respiration"
    assert "bad_secret_code" not in text
    assert "another script to strip" not in text
    assert "Hidden developer comment" not in text
    assert "font-size: 16px" not in text
    assert "Understanding Cellular Energy" in text
    assert "ATP is the primary energy carrier in cells & tissues." in text
    assert "Glycolysis occurs in cytoplasm." in text
    assert "Krebs cycle occurs in mitochondria." in text
    assert "Final summary paragraph." in text


def test_strip_html_content_empty():
    """strip_html_content on empty string returns empty strings."""
    assert strip_html_content("") == ("", "")
    assert strip_html_content(None) == ("", "")


def test_web_fetch_invalid_url():
    """web_fetch should return error on invalid schemes."""
    res1 = web_fetch("ftp://example.com")
    assert res1["status"] == 0
    assert "Invalid URL scheme" in res1["error"]

    res2 = web_fetch("javascript:void(0)")
    assert res2["status"] == 0
    assert "Invalid URL scheme" in res2["error"]

    res3 = web_fetch("")
    assert res3["status"] == 0
    assert "Invalid URL scheme" in res3["error"]


def test_web_fetch_success_mock():
    """web_fetch should extract title and readable text from HTTP 200 response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
    mock_resp.text = "<html><head><title>Mocked Title</title><script>var x = 1;</script></head><body><p>Hello World Content</p></body></html>"

    with patch("requests.get", return_value=mock_resp):
        res = web_fetch("https://example.com/test", max_chars=100)
        assert res["url"] == "https://example.com/test"
        assert res["title"] == "Mocked Title"
        assert "Hello World Content" in res["text"]
        assert "var x = 1" not in res["text"]
        assert res["status"] == 200
        assert res["error"] is None


def test_web_fetch_truncation():
    """web_fetch should truncate text to max_chars."""
    long_content = "A" * 500
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "text/plain"}
    mock_resp.text = long_content

    with patch("requests.get", return_value=mock_resp):
        res = web_fetch("https://example.com/long", max_chars=50)
        assert len(res["text"]) == 50
        assert res["text"] == "A" * 50


def test_web_fetch_http_error():
    """web_fetch should handle HTTP 404/500 errors gracefully."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.headers = {"Content-Type": "text/html"}
    mock_resp.text = "<html><body>Not Found</body></html>"

    with patch("requests.get", return_value=mock_resp):
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
    <html><body>
    <div class="result results_links results_links_deep web-result ">
        <a class="result__a" href="/l/?kh=-1&uddg=https%3A%2F%2Fwww.khanacademy.org%2Fmath%2Fcalculus">Introduction to Integral Calculus</a>
        <a class="result__snippet">Learn integral calculus with step by step examples and practice problems.</a>
    </div>
    <div class="result results_links results_links_deep web-result ">
        <a class="result__a" href="https://en.wikipedia.org/wiki/Integral">Integral - Wikipedia</a>
        <a class="result__snippet">In mathematics, an integral assigns numbers to functions in a way that describes displacement.</a>
    </div>
    </body></html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = sample_ddg_html

    with patch("requests.post", return_value=mock_resp):
        results = _search_ddg_html("integral calculus", max_results=5)
        assert len(results) == 2
        assert results[0]["title"] == "Introduction to Integral Calculus"
        assert results[0]["url"] == "https://www.khanacademy.org/math/calculus"
        assert "step by step" in results[0]["snippet"]
        assert results[1]["url"] == "https://en.wikipedia.org/wiki/Integral"


def test_search_wikipedia_api():
    """_search_wikipedia_api should parse Wikipedia OpenSearch API results."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "query": {
            "search": [
                {
                    "title": "Quantum mechanics",
                    "snippet": "Central branch of <span class=\"searchmatch\">physics</span> dealing with atomic scales.",
                }
            ]
        }
    }

    with patch("requests.get", return_value=mock_resp):
        results = _search_wikipedia_api("quantum mechanics", max_results=5)
        assert len(results) == 1
        assert results[0]["title"] == "Quantum mechanics"
        assert results[0]["url"] == "https://en.wikipedia.org/wiki/Quantum_mechanics"
        assert "<span" not in results[0]["snippet"]
        assert "Central branch" in results[0]["snippet"]


def test_search_arxiv_api():
    """_search_arxiv_api should parse arXiv Atom XML results."""
    sample_xml = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
        <entry>
            <id>http://arxiv.org/abs/2101.00001v1</id>
            <title>Attention Is All You Need</title>
            <summary>The dominant sequence transduction models are based on complex recurrent or convolutional neural networks.</summary>
        </entry>
    </feed>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = sample_xml.encode("utf-8")

    with patch("requests.get", return_value=mock_resp):
        results = _search_arxiv_api("transformer", max_results=5)
        assert len(results) == 1
        assert results[0]["title"] == "Attention Is All You Need"
        assert results[0]["url"] == "http://arxiv.org/abs/2101.00001v1"
        assert "dominant sequence" in results[0]["snippet"]


def test_web_search_fallback_cascade():
    """web_search falls back to Wikipedia when DDG fails, and arXiv when Wikipedia fails."""
    # Scenario: DDG fails, Wikipedia succeeds
    with patch("app.research._search_ddg_html", return_value=[]):
        with patch("app.research._search_wikipedia_api", return_value=[{"title": "Wiki Result", "url": "https://wiki.org", "snippet": "Snip"}]):
            res = web_search("test query")
            assert len(res) == 1
            assert res[0]["title"] == "Wiki Result"

    # Scenario: DDG and Wikipedia both fail, arXiv succeeds
    with patch("app.research._search_ddg_html", return_value=[]):
        with patch("app.research._search_wikipedia_api", return_value=[]):
            with patch("app.research._search_arxiv_api", return_value=[{"title": "Arxiv Paper", "url": "http://arxiv.org", "snippet": "Abstract"}]):
                res = web_search("test query")
                assert len(res) == 1
                assert res[0]["title"] == "Arxiv Paper"

    # Scenario: All raise exceptions
    with patch("app.research._search_ddg_html", side_effect=Exception("DDG error")):
        with patch("app.research._search_wikipedia_api", side_effect=Exception("Wiki error")):
            with patch("app.research._search_arxiv_api", side_effect=Exception("arXiv error")):
                res = web_search("test query")
                assert res == []
