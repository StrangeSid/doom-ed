# Contributing to doom-ed

Thank you for your interest in contributing to **doom-ed**!

---

## 📜 License & Guidelines

By contributing to this repository, you agree that your contributions will be licensed under the **GNU General Public License v3.0 or later** ([COPYING](COPYING)).

1. **Zero Synthetic / Demo Data**: Never introduce mock LMS data or fake course fixtures into production code paths. Either live data is available or the appropriate authentication/upstream error is raised.
2. **Academic Integrity Protection**: Never bypass or weaken `assert_not_live_test`. The study coach must never answer or submit live/unsubmitted tests.
3. **Encryption at Rest & Isolation**: All student tokens, L2 caches, private blobs, and page texts must remain encrypted at rest with Fernet and strictly isolated by `owner = f"{tenant}:{uid}"`.
4. **No Build Step on Web Client**: Keep `web/` pure vanilla HTML/CSS/ES modules. Don't add Webpack, Vite, or bundle steps for the client.

---

## 🛠️ Development Setup

```bash
# Clone the repository
git clone https://github.com/StrangeSid/doom-ed.git
cd doom-ed

# Create virtual environment and install dependencies
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt -r requirements-dev.txt

# Run the test suite
pytest
```

---

## 🧪 Testing Checklist

Before submitting a Pull Request, ensure:
- [ ] All unit and integration tests pass: `pytest`
- [ ] Real-browser Playwright tests pass: `pytest tests/test_e2e_playwright.py`
- [ ] Code follows PEP 8 conventions.
- [ ] New features include corresponding unit tests under `tests/`.
