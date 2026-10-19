# SPDX-License-Identifier: GPL-3.0-or-later
"""Playwright end-to-end browser tests for doom-ed web application."""

import socket
import threading
import time
from unittest.mock import MagicMock

import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from app.cache import clear as clear_cache
from app.main import app, db, session_store
from app.sessions import Session


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    clear_cache()
    port = _find_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to listen
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


def test_webapp_unauthenticated_flow(live_server, browser_context, monkeypatch):
    monkeypatch.setenv("DOOMED_AUTO_BOOTSTRAP_TOKEN", "0")
    context = browser_context.new_context()
    page = context.new_page()

    page.goto(live_server)
    page.wait_for_load_state("networkidle")

    # Title verification
    assert "doom-ed" in page.title().lower()

    # Verify unauthenticated state shows sign-in trigger
    login_btn = page.locator("#login-trigger")
    assert login_btn.is_visible()

    # Click sign in trigger opens modal
    login_btn.click()
    page.wait_for_selector("#login-modal[open]", timeout=3000)
    assert page.locator("#login-email").is_visible()

    # Cancel button closes modal
    page.locator("#login-cancel").click()
    page.wait_for_selector("#login-modal", state="hidden", timeout=3000)
    assert not page.locator("#login-modal").is_visible()

    context.close()


def test_webapp_authenticated_flow(live_server, browser_context, monkeypatch):
    import app.lms_bridge as bridge

    fake_lms = MagicMock()
    fake_lms.my_courses.return_value = {"courses": []}
    fake_lms.eol_tests.return_value = {
        "results": [
            {
                "id": "eol_test_42",
                "title": "Projectile Motion Check",
                "status": "completed",
                "score": 2,
                "total": 5,
                "subject": "Physics HL",
            }
        ]
    }
    fake_lms.eol_result.return_value = {
        "questions": [
            {"question_id": "q1", "text": "Maximum height formula", "is_correct": False, "score": 0}
        ]
    }
    fake_lms.assessments.return_value = {"results": []}
    fake_lms.learning_tasks.return_value = []
    fake_lms.calendar_events.return_value = {"events": []}
    fake_lms.notifications.return_value = {"notifications": []}
    fake_lms.announcements.return_value = {"announcements": []}
    monkeypatch.setattr(bridge, "lms", fake_lms)

    # Seed authenticated session
    sess = Session(
        access="token_playwright",
        refresh="refresh_playwright",
        tenant="tenant_e2e",
        name="Antigravity Tester",
        email="antigravity@school.edu",
        uid="u_anti_01",
        grade="Grade 11",
    )
    session_store.add(sess)

    context = browser_context.new_context()
    # Inject HttpOnly cookie
    context.add_cookies([
        {
            "name": "doomed_sid",
            "value": sess.sid,
            "domain": "127.0.0.1",
            "path": "/",
        }
    ])

    page = context.new_page()
    page.goto(live_server)
    page.wait_for_load_state("networkidle")

    # 1. Profile card rendered and attendance pill absent
    page.wait_for_selector("#profile-card:not([hidden])", timeout=5000)
    assert "Antigravity Tester" in page.locator("#profile-name").text_content()
    assert page.locator("#attendance-pill").count() == 0

    # 2. Fix List rendered from weak EOL test
    page.wait_for_selector(".fix-item", timeout=5000)
    assert "Projectile Motion Check" in page.locator(".fix-title").first.text_content()
    assert "2/5" in page.locator(".fix-score").first.text_content()

    # 3. New lesson creation
    page.locator("#new-lesson-btn").click()
    page.wait_for_timeout(500)
    assert page.locator("#composer-form").is_visible()

    # 4. Profile modal check (clicking profile card shows full student profile)
    page.locator("#profile-card").click()
    page.wait_for_selector("#profile-modal[open]", timeout=3000)
    assert "Antigravity Tester" in page.locator("#prof-name").text_content()
    page.locator("#profile-modal-close").click()
    page.wait_for_selector("#profile-modal", state="hidden", timeout=3000)

    # 5. Sidebar collapse & expand toggle
    page.locator("#sidebar-collapse-btn").click()
    page.wait_for_timeout(300)
    assert "left-collapsed" in (page.locator(".app-shell").get_attribute("class") or "")
    page.locator("#left-toggle").click()
    page.wait_for_timeout(300)
    assert "left-collapsed" not in (page.locator(".app-shell").get_attribute("class") or "")

    # 6. Lesson Log drawer toggle
    page.locator("#log-toggle").click()
    page.wait_for_selector("#log-drawer.open", timeout=3000)
    assert page.locator("#copy-log-btn").is_visible()
    page.locator("#log-close").click()
    page.wait_for_selector("#log-drawer:not(.open)", timeout=3000)

    # 7. Theme toggle
    theme_btn = page.locator("#theme-toggle")
    theme_btn.click()
    current_theme = page.locator("html").get_attribute("data-theme")
    assert current_theme in ("light", "dark")

    context.close()
