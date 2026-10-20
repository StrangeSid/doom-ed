# SPDX-License-Identifier: GPL-3.0-or-later
"""Playwright regression tests for stored-XSS handling in the web frontend.

LMS data and model/SSE output are untrusted. They must always be rendered as
text, never interpreted as HTML.
"""

import json
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from app.cache import clear as clear_cache
from app.main import app


XSS = '<img src=x onerror="window.__pwned=true">'


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fulfill_json(route, payload, status=200):
    route.fulfill(
        status=status,
        content_type="application/json",
        body=json.dumps(payload),
    )


@pytest.fixture(scope="module")
def live_server():
    clear_cache()
    port = _find_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    timeout = 10
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.1)

    yield base_url
    server.should_exit = True
    thread.join(timeout=3)
    clear_cache()


@pytest.fixture(scope="module")
def browser_context():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        yield browser
        browser.close()


def _study_payload():
    return {
        "status": "ok",
        "data": {
            "now": {
                "overdue": [{"title": XSS, "subject": XSS}],
                "due_soon": [{"title": XSS, "subject": XSS}],
                "calendar_today": [{"title": XSS}],
                "unread_notifications": [{"title": XSS}],
            },
            "weak_topics": [
                {
                    "id": "eol:1",
                    "subject": XSS,
                    "topic": XSS,
                    "title": XSS,
                    "score_label": XSS,
                    "why_weak": XSS,
                    "wrong_questions": [],
                }
            ],
            "before_break": [
                {
                    "id": "1",
                    "title": XSS,
                    "subject": XSS,
                    "due": "2026-10-25T00:00:00Z",
                    "type": "assignment",
                }
            ],
        },
    }


def _install_api_stubs(page):
    _ = page.route(
        "**/api/session",
        lambda route: _fulfill_json(
            route,
            {
                "status": "ok",
                "student": {
                    "name": "XSS Tester",
                    "email": "xss@school.example",
                    "grade": "Grade 11",
                    "subjects": [XSS],
                },
            },
        ),
    )
    _ = page.route(
        "**/api/threads",
        lambda route: _fulfill_json(
            route,
            {
                "status": "ok",
                "threads": [
                    {"id": "thread-1", "title": XSS, "phase": XSS},
                ],
            },
        ),
    )
    _ = page.route(
        "**/api/study*",
        lambda route: _fulfill_json(route, _study_payload()),
    )


def test_lms_payloads_render_as_text(live_server, browser_context):
    context = browser_context.new_context()
    page = context.new_page()
    _install_api_stubs(page)

    page.goto(live_server)
    page.wait_for_selector("#profile-card:not([hidden])", timeout=5000)
    page.wait_for_selector("#list-overdue li", timeout=5000)
    page.wait_for_selector(".fix-item", timeout=5000)
    page.wait_for_timeout(250)  # give a would-be onerror handler time to fire

    # No injected element executed.
    assert page.evaluate("() => window.__pwned") is None
    assert page.locator("img[onerror]").count() == 0
    assert page.locator("#list-overdue img").count() == 0
    assert page.locator("#fix-list img").count() == 0

    # Every triage section shows the payload as literal text.
    for selector in (
        "#list-overdue",
        "#list-due-soon",
        "#list-calendar-today",
        "#list-unread-notifs",
        "#fix-list",
        "#break-queue",
        "#thread-list",
    ):
        text = page.locator(selector).text_content() or ""
        assert "<img" in text, f"{selector} did not render the payload as text"

    # Untrusted subject list in the profile modal is text too.
    page.locator("#profile-card").click()
    page.wait_for_selector("#profile-modal[open]", timeout=3000)
    assert "<img" in (page.locator("#prof-subjects").text_content() or "")
    assert page.locator("#prof-subjects img").count() == 0
    page.locator("#profile-modal-close").click()

    assert page.evaluate("() => window.__pwned") is None
    context.close()


def test_renderer_fails_closed_without_parser_or_sanitizer(live_server, browser_context):
    context = browser_context.new_context()
    page = context.new_page()
    _install_api_stubs(page)
    page.goto(live_server)
    page.wait_for_function("() => window.__vendorReady !== undefined")
    page.evaluate("() => window.__vendorReady")

    results = page.evaluate(
        """async () => {
          const { renderMarkdown } = await import('/assets/renderer.js');
          const savedPurify = window.DOMPurify;
          const savedMarked = window.marked;
          const payload = '<img src=x onerror="window.__pwned=true">';
          const out = {};
          try {
            window.DOMPurify = undefined;
            out.noSanitizer = renderMarkdown(payload);

            window.marked = undefined;
            out.noParserOrSanitizer = renderMarkdown(payload + '\\nsecond line');

            window.DOMPurify = savedPurify;
            out.noParser = renderMarkdown(payload + '\\nsecond line');
          } finally {
            window.DOMPurify = savedPurify;
            window.marked = savedMarked;
          }
          return out;
        }"""
    )

    for key, rendered in results.items():
        assert "<img" not in rendered, f"{key} returned unescaped markup"
        assert "&lt;img" in rendered, f"{key} did not escape the payload"
    assert "<br" in results["noParserOrSanitizer"]
    assert "<br" in results["noParser"]
    assert page.evaluate("() => window.__pwned") is None
    context.close()
