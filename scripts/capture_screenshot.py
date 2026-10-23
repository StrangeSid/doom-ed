# SPDX-License-Identifier: GPL-3.0-or-later
"""Script to capture a high-DPI screenshot of the doom-ed web application in standalone mode."""

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

from app.cache import clear as clear_cache
from app.db import Database
from app.main import app, db, session_store
from app.sessions import Session


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def capture():
    clear_cache()
    port = _find_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    start = time.time()
    while time.time() - start < 10:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.1)

    # 1. Seed standalone session
    sess = Session(
        access="",
        refresh="",
        tenant="local",
        uid="standalone_hero",
        name="Alex Candidate",
        email="",
        grade="DP 2",
        curriculum="DP",
        subjects=[
            "Physics HL",
            "Mathematics: Analysis and Approaches HL",
            "Economics SL",
            "Chemistry SL",
            "English A Literature HL",
            "Spanish B SL",
        ],
        target_session="May 2026",
    )
    session_store.add(sess)

    # 2. Seed a thread and messages
    owner = sess.owner
    db.delete_thread(owner, "th_hero_1")
    t = db.create_thread(
        owner=owner,
        title="Single-Slit Diffraction & Path Difference",
        topic="Physics HL: Wave Optics",
        subject="Physics HL",
        thread_id="th_hero_1",
    )
    db.update_thread(owner, "th_hero_1", phase="teach")

    # Seed an unpassed quiz in database so weak topics shows it
    db.create_quiz_state(
        call_id="call_quiz_optics_1",
        thread_id="th_hero_1",
        owner=owner,
        question="What is the path difference between wavelets from the top edge and center of a slit of width b at angle theta?",
        options=["(b/2) sin(theta)", "b sin(theta)", "(b/2) cos(theta)", "b lambda"],
        correct=[0],
        explanation="Dividing the slit into two halves of width b/2 gives path difference (b/2) sin(theta) between corresponding rays.",
    )
    db.record_quiz_answer(
        call_id="call_quiz_optics_1",
        owner=owner,
        user_answer=[1],
        grade_result={
            "correct": False,
            "dontKnow": False,
            "correct_options": [0],
            "explanation": "Path difference is (b/2) sin(theta).",
        },
    )

    # Add messages
    user_q = "Teach me single-slit diffraction from first principles. Where does the condition for the first minimum come from?"
    db.add_message("th_hero_1", "user", user_q)

    assistant_reply = r"""Every point on an unblocked wavefront of aperture $b$ acts as a coherent secondary wavelet source (Huygens' Principle). Pairing each wavelet in the top half $[0, b/2]$ with its partner $b/2$ below gives path difference:

$$\Delta r = \frac{b}{2} \sin \theta$$

For complete destructive interference at viewing angle $\theta$, every paired wavelet must arrive out of phase ($\Delta r = \frac{\lambda}{2}$):

$$\frac{b}{2} \sin \theta = \frac{\lambda}{2} \implies b \sin \theta = \lambda \implies \theta \approx \frac{\lambda}{b}$$"""
    db.add_message("th_hero_1", "assistant", assistant_reply)

    # Launch browser with retina scale
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        # 1440x900 at 2x scale = 2880x1800 (clean retina resolution)
        context = browser.new_context(
            viewport={"width": 1440, "height": 960},
            device_scale_factor=2,
            color_scheme="dark",
        )
        context.add_cookies([
            {
                "name": "doomed_sid",
                "value": sess.sid,
                "domain": "127.0.0.1",
                "path": "/",
            }
        ])

        page = context.new_page()
        page.goto(base_url)
        page.wait_for_load_state("networkidle")

        # Wait for thread list and triage to render
        page.wait_for_selector(".thread-item", timeout=5000)
        # Select the thread
        page.click(".thread-item")
        time.sleep(1.0)

        # Inject attached file chip and active quiz card into the chat
        page.evaluate("""async () => {
            // 1. Ensure left pane is scrolled to top to show profile card and triage
            const leftPane = document.getElementById('left-pane');
            if (leftPane) leftPane.scrollTop = 0;

            // 2. Add attached file chip in tray
            const attachedTray = document.getElementById('attached-tray');
            if (attachedTray) {
                attachedTray.innerHTML = `
                    <button class="file-chip" type="button" title="Physics_Data_Booklet_2026.pdf">
                        <span class="chip-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="13" height="13"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg></span>
                        <span class="chip-name">Physics_Data_Booklet_2026.pdf</span>
                    </button>
                    <button class="file-chip" type="button" title="Physics_HL_Paper_2_TZ2.pdf">
                        <span class="chip-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="13" height="13"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg></span>
                        <span class="chip-name">Physics_HL_Paper_2_TZ2.pdf</span>
                    </button>
                `;
            }

            // 3. Render authentic quiz card
            const { renderQuizCard } = await import('/assets/cards.js');
            const cardData = {
                card_type: 'quiz',
                call_id: 'call_live_check_1',
                question: 'If the slit width $b$ is halved while keeping wavelength $\\\\lambda$ constant, what happens to the angular half-width $\\\\theta$ to the first minimum?',
                options: [
                    { index: 1, label: 'The angular half-width doubles ($\\\\theta = 2\\\\lambda / b$)' },
                    { index: 2, label: 'The angular half-width is halved ($\\\\theta = \\\\lambda / 2b$)' },
                    { index: 3, label: 'The central maximum intensity doubles but angular width is constant' }
                ]
            };
            const mount = document.createElement('div');
            mount.className = 'chat-card-mount';
            const quizEl = renderQuizCard(cardData, () => {});
            mount.appendChild(quizEl);
            const msgList = document.getElementById('message-list');
            if (msgList) {
                msgList.appendChild(mount);
                msgList.scrollTop = 140;
            }
        }""")

        time.sleep(1.5)

        out_path = Path("/Users/sid/Documents/proj/doom-ed/docs/assets/homepage.png")
        page.screenshot(path=str(out_path))
        print(f"Screenshot written to {out_path}")

        context.close()
        browser.close()

    server.should_exit = True
    thread.join(timeout=3)


if __name__ == "__main__":
    capture()
