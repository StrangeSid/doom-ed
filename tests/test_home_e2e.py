# SPDX-License-Identifier: GPL-3.0-or-later
"""Playwright coverage for the home feed (flag on).

These tests use a unique local owner and a unique subject so they do not
collide with other browser suites or with any real practice data. Run with the
practice and home-feed flags enabled (the fixture sets them for the server):

    .venv/bin/pytest tests/test_home_e2e.py -q

The file is skipped if Playwright or the Chrome channel is unavailable.
"""

from __future__ import annotations

import socket
import threading
import time
import uuid

import pytest
import uvicorn

from app.cache import clear as clear_cache
from app.main import app, db, practice_service, session_store
from app.practice.models import Item, Skill, StudentState
from app.sessions import Session

try:  # Playwright is a dev dependency; keep collection friendly without it.
    from playwright.sync_api import sync_playwright

    PLAYWRIGHT_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only in minimal environments
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not PLAYWRIGHT_AVAILABLE, reason="playwright is not installed"
)

RUN_ID = uuid.uuid4().hex[:10]
STUDENT_UID = f"home-e2e-{RUN_ID}"
SUBJECT = f"Home E2E Physics {RUN_ID}"
SKILL_ID = f"home.e2e.{RUN_ID}"
ITEM_PREFIX = f"home-e2e-{RUN_ID}-item-"
OWNER = f"local:{STUDENT_UID}"
CARD_COUNT = 8


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _seed_practice() -> None:
    store = practice_service.store
    store.upsert_skill(
        Skill(
            id=SKILL_ID,
            subject=SUBJECT,
            title="Projectile motion",
            unit="Mechanics",
            description="Horizontal launches and time of flight.",
            exam_weight=0.8,
        )
    )
    now = time.time()
    # Evidence on the skill means the planner sees a returning learner, not a
    # brand-new skill (which has a lower daily item cap).
    store.upsert_state(
        StudentState(
            owner=OWNER,
            skill_id=SKILL_ID,
            mastery=0.42,
            attempts=3,
            correct=1,
            last_seen=now - 4 * 86400,
            due_at=now - 120,
            stability=1.2,
        )
    )
    for index in range(CARD_COUNT):
        store.insert_item(
            Item(
                id=f"{ITEM_PREFIX}{index}",
                skill_ids=(SKILL_ID,),
                format="mcq_single",
                stem=(
                    f"E2E check {index + 1}: a ball is launched horizontally at "
                    f"{4 + index} m/s from a {20 + index} m cliff. "
                    "How long is it in the air? Use $g = 10$ m/s²."
                ),
                options=(
                    {"index": 1, "label": "2 s", "value": "2"},
                    {"index": 2, "label": "3 s", "value": "3"},
                    {"index": 3, "label": "4 s", "value": "4"},
                ),
                solution={"correct": [1], "explanation": "Use $h = \\frac{1}{2} g t^2$."},
                owner=OWNER,
                difficulty=0.0,
                verification="verified",
                content_hash=f"{RUN_ID}-hash-{index}",
            )
        )


def _cleanup() -> None:
    try:
        practice_service.store.reset_practice(OWNER)
    except Exception:
        pass
    for statement, params in (
        ("DELETE FROM item_stats WHERE item_id LIKE ?", (ITEM_PREFIX + "%",)),
        ("DELETE FROM items WHERE owner = ?", (OWNER,)),
        ("DELETE FROM skills WHERE id = ?", (SKILL_ID,)),
        ("DELETE FROM threads WHERE owner = ?", (OWNER,)),
        ("DELETE FROM item_reports WHERE owner = ?", (OWNER,)),
    ):
        try:
            db.execute(statement, params)
        except Exception:
            pass


@pytest.fixture(scope="module")
def home_server():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DOOMED_PRACTICE_ENGINE", "1")
    monkeypatch.setenv("DOOMED_HOME_FEED", "1")
    monkeypatch.setenv("DOOMED_AUTO_BOOTSTRAP_TOKEN", "0")
    clear_cache()

    _seed_practice()
    session = Session(
        access="",
        refresh="",
        tenant="local",
        name=f"Home E2E {RUN_ID}",
        email="",
        uid=STUDENT_UID,
        grade="DP 2",
        subjects=[SUBJECT],
        target_session="May 2027",
    )
    session_store.add(session)

    port = _find_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.1)
    else:  # pragma: no cover - server failed to boot
        session_store.drop(session.sid)
        monkeypatch.undo()
        pytest.fail("The test server did not start.")

    try:
        yield {"base_url": base_url, "session": session}
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        clear_cache()
        _cleanup()
        session_store.drop(session.sid)
        monkeypatch.undo()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # pragma: no cover - depends on the host
            pytest.skip(f"Chrome is not available for Playwright: {exc}")
        yield instance
        instance.close()


def _new_context(browser, home_server, **kwargs):
    context = browser.new_context(**kwargs)
    context.add_cookies(
        [
            {
                "name": "doomed_sid",
                "value": home_server["session"].sid,
                "domain": "127.0.0.1",
                "path": "/",
            }
        ]
    )
    return context


def _collect_console_error(message, sink):
    """Record real console errors; the app's CSP meta tag emits one known warning."""
    if message.type != "error":
        return
    text = message.text or ""
    if "Content Security Policy" in text and "frame-ancestors" in text:
        return
    sink.append(text)


def _fulfill_chat_stream(route):
    """A minimal SSE reply so the teach handoff never calls a real model."""
    body = (
        'event: token\n'
        'data: {"content":"Let us start from the foundations."}\n\n'
        'event: done\n'
        'data: {}\n\n'
    )
    route.fulfill(
        status=200,
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        body=body,
    )


def test_home_feed_answer_persist_load_more_and_teach(home_server, browser):
    context = _new_context(browser, home_server, viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("console", lambda message: _collect_console_error(message, page_errors))
    page.route("**/api/chat/send", _fulfill_chat_stream)

    try:
        page.goto(home_server["base_url"])
        page.wait_for_load_state("networkidle")

        # 1. The feed is the landing view and renders server-graded cards.
        page.wait_for_selector("#home-view:not([hidden])", timeout=8000)
        assert page.locator("#center-pane").is_hidden()
        page.wait_for_selector(".feed-card", timeout=8000)
        assert page.locator(".feed-card").count() >= 1
        assert "Projectile motion" in (page.locator(".feed-skill").first.text_content() or "")
        assert (page.locator("#home-greeting").text_content() or "").strip()
        assert "/" in (page.locator("#home-progress-count").text_content() or "")

        # 2. "Why this card?" is a real disclosure with the planner reason.
        page.locator(".feed-why summary").first.click()
        assert page.locator(".feed-why-text").first.is_visible()

        # 3. Answer the first card (option index 1 is the seeded correct one;
        #    index 0 is the pinned "I don't know").
        card = page.locator(".feed-card").first
        card.locator(".dcard-opt").nth(1).click()
        page.wait_for_selector(".feed-card .dcard-grade", timeout=8000)
        grade = page.locator(".feed-card .dcard-grade").first.text_content() or ""
        assert "Correct" in grade
        assert (page.locator(".feed-state-chip").first.text_content() or "").strip() == "Answered"
        assert (page.locator("#home-progress-count").text_content() or "").startswith("1 /")

        # 4. Reload keeps the server-side plan and the answered state.
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#home-view:not([hidden])", timeout=8000)
        page.wait_for_selector(".feed-card.is-answered", timeout=8000)
        assert page.locator(".feed-card.is-answered").count() >= 1
        assert page.locator(".feed-card.is-answered .feed-answered-text").first.is_visible()

        # 5. Cursor pagination through the visible Load more fallback.
        assert page.locator("#home-load-more").is_visible()
        page.locator("#home-load-more").click()
        page.wait_for_function(
            "() => document.querySelectorAll('.feed-card').length >= 8", timeout=8000
        )
        assert page.locator(".feed-card").count() == CARD_COUNT

        # 6. Teach handoff: server creates a lesson, app switches views, seeds
        #    the composer, and sends through the existing SSE endpoint.
        page.locator(".feed-card .feed-action[data-action='teach']").first.click()
        page.wait_for_selector("#center-pane:not([hidden])", timeout=8000)
        assert "#/lesson/" in page.url
        page.wait_for_selector(".msg-row.msg-user", timeout=8000)
        assert "I need to fix" in (page.locator(".msg-row.msg-user").last.text_content() or "")
        page.wait_for_selector(".msg-row.msg-assistant", timeout=8000)
        assert "foundations" in (
            page.locator(".msg-row.msg-assistant").last.text_content() or ""
        )

        assert page_errors == [], f"uncaught page errors: {page_errors}"
    finally:
        context.close()


def test_home_feed_mobile_reaches_triage_and_composer(home_server, browser):
    context = _new_context(
        browser,
        home_server,
        viewport={"width": 390, "height": 844},
        is_mobile=True,
        has_touch=True,
    )
    page = context.new_page()
    try:
        page.goto(home_server["base_url"])
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(".feed-card", timeout=8000)

        # The home sidebar toggle opens the triage overlay on a phone.
        assert page.locator("#home-sidebar-toggle").is_visible()
        page.locator("#home-sidebar-toggle").click()
        page.wait_for_selector("#left-pane.open", timeout=3000)
        page.locator("#scrim").click(position={"x": 380, "y": 300})
        page.wait_for_selector("#left-pane:not(.open)", timeout=3000)

        # Lessons is one tap away and the composer is reachable.
        page.locator("#home-nav-lessons").click()
        page.wait_for_selector("#center-pane:not([hidden])", timeout=5000)
        assert page.locator("#composer-form").is_visible()
        page.locator("#prompt-input").scroll_into_view_if_needed()
        assert page.locator("#prompt-input").is_visible()

        # The chat header keeps Home reachable on mobile.
        assert page.locator("#chat-nav-home").is_visible()
        page.locator("#chat-nav-home").click()
        page.wait_for_selector("#home-view:not([hidden])", timeout=5000)
        assert page.locator(".feed-card").first.is_visible()
    finally:
        context.close()


def test_home_feed_calibration_state_for_empty_bank(home_server, browser):
    """A fresh owner with no verified items sees the calibration state, not fake cards."""
    fresh = Session(
        access="",
        refresh="",
        tenant="local",
        name="Fresh Student",
        email="",
        uid=f"home-empty-{RUN_ID}",
        grade="DP 2",
        subjects=[f"Empty Subject {RUN_ID}"],
        target_session="May 2027",
    )
    session_store.add(fresh)
    context = browser.new_context(viewport={"width": 1100, "height": 800})
    context.add_cookies(
        [
            {
                "name": "doomed_sid",
                "value": fresh.sid,
                "domain": "127.0.0.1",
                "path": "/",
            }
        ]
    )
    page = context.new_page()
    try:
        page.goto(home_server["base_url"])
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#home-empty:not([hidden])", timeout=8000)
        text = page.locator("#home-empty").text_content() or ""
        assert "Preparing your practice" in text
        assert page.locator(".feed-card").count() == 0
    finally:
        context.close()
        try:
            practice_service.store.reset_practice(fresh.owner)
        finally:
            session_store.drop(fresh.sid)


def test_home_feed_404_falls_back_to_lesson(home_server, browser):
    """A flag mismatch (UI on, engine off) must not strand the student."""
    context = _new_context(browser, home_server, viewport={"width": 1100, "height": 800})
    page = context.new_page()

    def _not_found(route):
        route.fulfill(
            status=404,
            content_type="application/json",
            body='{"detail":"Practice engine is disabled."}',
        )

    page.route("**/api/home/feed*", _not_found)
    try:
        page.goto(home_server["base_url"] + "/#/home")
        page.wait_for_selector("#center-pane:not([hidden])", timeout=8000)
        assert page.locator("#home-view").is_hidden()
        assert "#/lesson" in page.url
        assert page.locator("#composer-form").is_visible()
    finally:
        context.close()


def test_home_feed_skips_and_reports_without_breaking_layout(home_server, browser):
    """Skip hides locally; report posts item feedback. Both stay in the feed DOM model."""
    context = _new_context(browser, home_server, viewport={"width": 1280, "height": 900})
    page = context.new_page()
    events = []
    page.on(
        "request",
        lambda request: events.append(request.url)
        if request.method == "POST" and "/api/" in request.url
        else None,
    )
    try:
        page.goto(home_server["base_url"])
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(".feed-card", timeout=8000)

        target = page.locator(".feed-card").first
        target.locator(".feed-report .feed-action").first.click()
        target.locator(".feed-report-menu .feed-action").nth(1).click()
        page.wait_for_timeout(600)
        assert any("/api/practice/feedback" in url for url in events)

        skip_target = page.locator(".feed-card").nth(1)
        skip_target.locator(".feed-action[data-action='skip']").click()
        assert skip_target.is_hidden()
        assert "Skipped" in (page.locator("#home-status").text_content() or "")
    finally:
        context.close()
