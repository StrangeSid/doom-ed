# SPDX-License-Identifier: GPL-3.0-or-later
"""Playwright end-to-end browser tests for doom-ed web application."""

from unittest.mock import MagicMock

import pytest

from app.cache import clear as clear_cache
from app.main import app, session_store
from app.sessions import Session
from tests.browser_helpers import (
    PLAYWRIGHT_AVAILABLE,
    launch_chromium_or_skip,
    managed_browser_context,
    start_uvicorn_server,
    sync_playwright,
)

pytestmark = pytest.mark.skipif(
    not PLAYWRIGHT_AVAILABLE, reason="playwright is not installed"
)


@pytest.fixture(scope="module")
def live_server():
    clear_cache()
    try:
        with start_uvicorn_server(app) as base_url:
            yield base_url
    finally:
        clear_cache()


@pytest.fixture(scope="module")
def browser_context():
    with sync_playwright() as p:
        browser = launch_chromium_or_skip(p)
        try:
            yield browser
        finally:
            browser.close()


def test_webapp_unauthenticated_flow(browser_context, live_server, monkeypatch, request):
    monkeypatch.setenv("DOOMED_AUTO_BOOTSTRAP_TOKEN", "0")
    with managed_browser_context(browser_context, request) as context:
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


def test_webapp_authenticated_flow(browser_context, live_server, monkeypatch, request):
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
    try:
        with managed_browser_context(browser_context, request) as context:
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
    finally:
        session_store.drop(sess.sid)


def test_chat_card_state_and_action_styles(browser_context, live_server, monkeypatch, request):
    """Chat-card overrides must not erase grades or promote secondary actions."""
    monkeypatch.setenv("DOOMED_AUTO_BOOTSTRAP_TOKEN", "0")
    with managed_browser_context(browser_context, request) as context:
        page = context.new_page()
        page.emulate_media(color_scheme="light")
        page.add_init_script("localStorage.setItem('doomed.theme', 'light')")
        page.goto(live_server)
        page.wait_for_load_state("networkidle")

        styles = page.evaluate(
            """async () => {
              const { renderQuizCard, renderWriteConfirmCard } = await import('/assets/cards.js');
              const messageList = document.getElementById('message-list');

              const quizMount = document.createElement('div');
              quizMount.className = 'chat-card-mount';
              messageList.appendChild(quizMount);
              const quiz = renderQuizCard({
                question: 'State test',
                options: [{index: 1, label: 'Correct'}, {index: 2, label: 'Wrong'}],
              }, () => {});
              quizMount.appendChild(quiz);
              quiz.querySelector('[data-index="2"]').classList.add('is-chosen');
              quiz.applyGrade({correct: false, correct_options: [1]});

              const writeMount = document.createElement('div');
              writeMount.className = 'chat-card-mount';
              messageList.appendChild(writeMount);
              writeMount.appendChild(renderWriteConfirmCard({action: 'submit_feedback'}, () => {}));

              const correct = quiz.querySelector('.is-correct');
              const wrong = quiz.querySelector('.is-wrong');
              const confirm = writeMount.querySelector('.dcard-btn:not(.ghost)');
              const cancel = writeMount.querySelector('.dcard-btn.ghost');
              const css = (el, property) => getComputedStyle(el)[property];
              return {
                correctBackground: css(correct, 'backgroundColor'),
                correctBorder: css(correct, 'borderColor'),
                correctMark: css(correct.querySelector('.dcard-opt-mark'), 'backgroundColor'),
                wrongBackground: css(wrong, 'backgroundColor'),
                wrongBorder: css(wrong, 'borderColor'),
                wrongMark: css(wrong.querySelector('.dcard-opt-mark'), 'backgroundColor'),
                confirmBackground: css(confirm, 'backgroundColor'),
                cancelBackground: css(cancel, 'backgroundColor'),
                cancelColor: css(cancel, 'color'),
                cancelBorder: css(cancel, 'borderColor'),
              };
            }"""
        )

        assert styles["correctBackground"] == "rgb(224, 235, 228)"
        assert styles["correctBorder"] == "rgb(42, 99, 64)"
        assert styles["correctMark"] == "rgb(42, 99, 64)"
        assert styles["wrongBackground"] == "rgb(241, 226, 223)"
        assert styles["wrongBorder"] == "rgb(160, 57, 42)"
        assert styles["wrongMark"] == "rgb(160, 57, 42)"
        assert styles["confirmBackground"] == "rgb(160, 134, 114)"
        assert styles["cancelBackground"] == "rgb(242, 242, 242)"
        assert styles["cancelColor"] == "rgb(17, 17, 17)"
        assert styles["cancelBorder"] == "rgb(184, 184, 184)"

        page.set_viewport_size({"width": 390, "height": 844})
        accessibility = page.evaluate(
            """() => {
              document.documentElement.dataset.theme = 'dark';
              const toast = document.createElement('div');
              toast.className = 'toast error';
              toast.textContent = 'Error';
              document.getElementById('toasts').appendChild(toast);

              const fixture = document.createElement('div');
              fixture.style.cssText = 'position:fixed;top:0;left:0;z-index:1000;display:flex';
              fixture.innerHTML = `
                <button class="feed-action">Act</button>
                <details class="feed-why"><summary>Why</summary></details>
                <span class="file-chip"><button class="chip-rm">×</button></span>
                <div class="visual-zoom"><button>+</button></div>
                <button class="thread-delete">×</button>`;
              document.body.appendChild(fixture);
              const box = selector => {
                const r = fixture.querySelector(selector).getBoundingClientRect();
                return {width: r.width, height: r.height};
              };
              return {
                toastColor: getComputedStyle(toast).color,
                toastBackground: getComputedStyle(toast).backgroundColor,
                feedAction: box('.feed-action'),
                feedWhy: box('.feed-why summary'),
                chipRemove: box('.chip-rm'),
                visualZoom: box('.visual-zoom button'),
                threadDelete: box('.thread-delete'),
              };
            }"""
        )
        assert accessibility["toastColor"] == "rgb(28, 28, 28)"
        assert accessibility["toastBackground"] == "rgb(232, 152, 138)"
        for selector in ("feedAction", "feedWhy", "chipRemove", "visualZoom", "threadDelete"):
            assert accessibility[selector]["height"] >= 44, selector
        for selector in ("chipRemove", "visualZoom", "threadDelete"):
            assert accessibility[selector]["width"] >= 44, selector
