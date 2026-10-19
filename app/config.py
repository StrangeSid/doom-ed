# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuration constants, secret key management, and runtime settings for doom-ed."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

_PLACEHOLDER_SECRET = "change-me-to-a-32-byte-secret-passphrase"


def ensure_data_dir(path: str | Path | None = None) -> Path:
    """Resolve and create the data directory with 0700 permissions."""
    raw = path if path is not None else os.environ.get("DOOMED_DATA_DIR", "./data")
    resolved = Path(raw).expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(resolved, 0o700)
    except OSError:
        pass
    for sub in ("blobs/private", "blobs/ib", "renders", "viz"):
        sub_path = resolved / sub
        sub_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(sub_path, 0o700)
        except OSError:
            pass
    return resolved


DATA_DIR: Path = ensure_data_dir()
SESSIONS_DB_PATH: Path = DATA_DIR / "sessions.sqlite"
DOOMED_DB_PATH: Path = DATA_DIR / "doomed.sqlite"
PRIVATE_BLOBS_DIR: Path = DATA_DIR / "blobs" / "private"
IB_BLOBS_DIR: Path = DATA_DIR / "blobs" / "ib"
RENDERS_DIR: Path = DATA_DIR / "renders"
VIZ_DIR: Path = DATA_DIR / "viz"


def derive_fernet_key(secret: str | bytes) -> bytes:
    """Convert a raw secret or passphrase into a valid 32-byte urlsafe-base64 Fernet key."""
    raw = secret.strip() if isinstance(secret, str) else secret.strip()
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8")
    else:
        raw_bytes = raw
    if not raw_bytes:
        raise ValueError("Secret key cannot be empty")
    try:
        Fernet(raw_bytes)
        return raw_bytes
    except Exception:
        digest = hashlib.sha256(raw_bytes).digest()
        return base64.urlsafe_b64encode(digest)


def load_or_create_secret_key(data_dir: Path | None = None) -> bytes:
    """Load DOOMED_SECRET_KEY from environment or persist a generated .secret_key (0600) in DATA_DIR."""
    target_dir = ensure_data_dir(data_dir) if data_dir is not None else DATA_DIR
    env_val = os.environ.get("DOOMED_SECRET_KEY", "").strip()
    if env_val and env_val != _PLACEHOLDER_SECRET:
        return derive_fernet_key(env_val)

    key_file = target_dir / ".secret_key"
    if key_file.exists():
        try:
            stored = key_file.read_text(encoding="utf-8").strip()
            if stored:
                try:
                    os.chmod(key_file, 0o600)
                except OSError:
                    pass
                return derive_fernet_key(stored)
        except OSError:
            pass

    generated = Fernet.generate_key()
    fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, generated + b"\n")
    finally:
        os.close(fd)
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return generated


SECRET_KEY: bytes = load_or_create_secret_key(DATA_DIR)

# --- config.json (LLM backend) ----------------------------------------------
# Project root config file: LLM base_url/api_key/models live here so the backend
# endpoint can be changed without touching code. Override the path with
# DOOMED_CONFIG_FILE if needed.
ROOT_DIR: Path = Path(__file__).resolve().parent.parent
CONFIG_FILE: Path = Path(
    os.environ.get("DOOMED_CONFIG_FILE") or (ROOT_DIR / "config.json")
).expanduser()

_DEFAULT_LLM_CONFIG: dict[str, str] = {
    "base_url": os.environ.get("LLM_BASE_URL", "http://localhost:20128/v1"),
    "api_key": os.environ.get("LLM_API_KEY", os.environ.get("OMNIROUTE_API_KEY", "")),
    "teacher_model": os.environ.get("TEACHER_MODEL", "agy/gemini-3.8-flash-high"),
    "maker_model": os.environ.get("MAKER_MODEL", "agy/gemini-3.8-flash-high"),
    "researcher_model": os.environ.get("RESEARCHER_MODEL", "agy/gemini-3.8-flash-high"),
}
_LLM_FILE_KEYS: dict[str, str] = {
    "LLM_BASE_URL": "base_url",
    "LLM_API_KEY": "api_key",
    "TEACHER_MODEL": "teacher_model",
    "MAKER_MODEL": "maker_model",
    "RESEARCHER_MODEL": "researcher_model",
}


def load_config_file(path: str | Path | None = None) -> dict[str, Any]:
    """Load config.json contents (or {} when missing/unreadable/invalid)."""
    target = Path(path).expanduser() if path is not None else CONFIG_FILE
    if not target.exists() or not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_config_file(data: dict[str, Any], path: str | Path | None = None) -> None:
    """Merge `data` into config.json atomically (0600 permissions; may hold API keys)."""
    target = Path(path).expanduser() if path is not None else CONFIG_FILE
    merged = dict(load_config_file(target))
    merged.update(data)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(f"{target.name}.tmp")
        tmp_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def _load_llm_settings() -> dict[str, str]:
    """Effective LLM defaults: config.json layered over environment defaults.
    Creates config.json from the environment/defaults when it does not exist."""
    if not CONFIG_FILE.exists():
        save_config_file({"llm": dict(_DEFAULT_LLM_CONFIG)})
    file_cfg = load_config_file()
    file_llm = file_cfg.get("llm") if isinstance(file_cfg.get("llm"), dict) else {}
    settings = dict(_DEFAULT_LLM_CONFIG)
    for key in _DEFAULT_LLM_CONFIG:
        val = file_llm.get(key)
        if isinstance(val, str) and val.strip():
            settings[key] = val.strip()
    return settings


_LLM_SETTINGS: dict[str, str] = _load_llm_settings()

# OpenAI-compatible LLM configuration (config.json wins over environment defaults)
LLM_BASE_URL: str = _LLM_SETTINGS["base_url"].rstrip("/")
LLM_API_KEY: str = _LLM_SETTINGS["api_key"]
TEACHER_MODEL: str = _LLM_SETTINGS["teacher_model"]
MAKER_MODEL: str = _LLM_SETTINGS["maker_model"]
RESEARCHER_MODEL: str = _LLM_SETTINGS["researcher_model"]

# Indus LMS optional local CLI token path
INDUSLMS_TOKEN_FILE: Path = Path(
    os.environ.get("INDUSLMS_TOKEN_FILE", "~/.induslms_token.json")
).expanduser()

# IB Resource Catalog Sidecar
IB_SCRAPE_BASE_URL: str = os.environ.get(
    "IB_SCRAPE_BASE_URL", "https://ib-scrape.sidevv.xyz"
).rstrip("/")
ALLOW_LOCAL_IB: bool = os.environ.get("DOOMED_ALLOW_LOCAL_IB", "").lower() in (
    "1",
    "true",
    "yes",
) or IB_SCRAPE_BASE_URL.startswith(("http://localhost:8471", "http://127.0.0.1:8471"))

# Cache TTLs (seconds)
DATA_TTL: int = int(os.environ.get("DOOMED_DATA_TTL", "300"))
FILES_TTL: int = int(os.environ.get("DOOMED_FILES_TTL", "3600"))
DETAIL_TTL: int = int(os.environ.get("DOOMED_DETAIL_TTL", "300"))
PAYLOAD_PERSIST_TTL: int = int(os.environ.get("DOOMED_PERSIST_DATA_TTL", "3600"))
FILES_PERSIST_TTL: int = int(os.environ.get("DOOMED_PERSIST_FILES_TTL", "86400"))
CACHE_SIZE: int = int(os.environ.get("DOOMED_CACHE_SIZE", "200"))

# Storage & file size limits
MAX_FILE_BYTES: int = int(os.environ.get("DOOMED_MAX_FILE_BYTES", str(25 * 1024 * 1024)))
MAX_PRIVATE_CACHE_MB: int = int(os.environ.get("DOOMED_MAX_PRIVATE_CACHE_MB", "512"))
MAX_IB_CACHE_MB: int = int(os.environ.get("DOOMED_MAX_IB_CACHE_MB", "1024"))

_CONFIG_LOCK = threading.Lock()
_RUNTIME_OVERRIDES: dict[str, str] = {}
_DEFAULTS: dict[str, str] = {
    "LLM_BASE_URL": LLM_BASE_URL,
    "LLM_API_KEY": LLM_API_KEY,
    "TEACHER_MODEL": TEACHER_MODEL,
    "MAKER_MODEL": MAKER_MODEL,
    "RESEARCHER_MODEL": RESEARCHER_MODEL,
    "IB_SCRAPE_BASE_URL": IB_SCRAPE_BASE_URL,
}


def _persist_runtime_override(key: str, value: str, db_path: Path | None = None) -> None:
    target = db_path or DOOMED_DB_PATH
    try:
        conn = sqlite3.connect(str(target), timeout=5.0)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS runtime_config "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            conn.execute(
                "INSERT INTO runtime_config (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except Exception:
        pass


def _load_runtime_overrides(db_path: Path | None = None) -> dict[str, str]:
    target = db_path or DOOMED_DB_PATH
    if not target.exists():
        return {}
    try:
        conn = sqlite3.connect(str(target), timeout=5.0)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS runtime_config "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            rows = conn.execute("SELECT key, value FROM runtime_config").fetchall()
            return {str(k): str(v) for k, v in rows}
        finally:
            conn.close()
    except Exception:
        return {}


def get_setting(key: str, default: str | None = None) -> str:
    """Return runtime setting override if set, else environment/module default."""
    with _CONFIG_LOCK:
        if key in _RUNTIME_OVERRIDES:
            return _RUNTIME_OVERRIDES[key]
        persisted = _load_runtime_overrides()
        if persisted:
            _RUNTIME_OVERRIDES.update(persisted)
            if key in _RUNTIME_OVERRIDES:
                return _RUNTIME_OVERRIDES[key]
    if default is not None:
        return default
    return _DEFAULTS.get(key, os.environ.get(key, ""))


def set_setting(key: str, value: str, persist: bool = True) -> None:
    """Update a runtime setting in memory and optionally in SQLite."""
    with _CONFIG_LOCK:
        _RUNTIME_OVERRIDES[key] = value
        if persist:
            _persist_runtime_override(key, value)


def get_llm_config() -> dict[str, str]:
    """Return effective LLM settings (with any runtime overrides applied)."""
    return {
        "base_url": get_setting("LLM_BASE_URL", LLM_BASE_URL).rstrip("/"),
        "api_key": get_setting("LLM_API_KEY", LLM_API_KEY),
        "teacher_model": get_setting("TEACHER_MODEL", TEACHER_MODEL),
        "maker_model": get_setting("MAKER_MODEL", MAKER_MODEL),
        "researcher_model": get_setting("RESEARCHER_MODEL", RESEARCHER_MODEL),
    }


def update_llm_config(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    teacher_model: str | None = None,
    maker_model: str | None = None,
    researcher_model: str | None = None,
    persist: bool = True,
    **extra: Any,
) -> dict[str, str]:
    """Update one or more runtime LLM settings and return the effective configuration."""
    mapping = {
        "LLM_BASE_URL": base_url if base_url is not None else extra.get("LLM_BASE_URL"),
        "LLM_API_KEY": api_key if api_key is not None else extra.get("LLM_API_KEY"),
        "TEACHER_MODEL": teacher_model if teacher_model is not None else extra.get("TEACHER_MODEL"),
        "MAKER_MODEL": maker_model if maker_model is not None else extra.get("MAKER_MODEL"),
        "RESEARCHER_MODEL": (
            researcher_model if researcher_model is not None else extra.get("RESEARCHER_MODEL")
        ),
    }
    file_updates: dict[str, str] = {}
    for env_key, v in mapping.items():
        if v is None:
            continue
        clean = str(v).strip()
        set_setting(env_key, clean, persist=persist)
        file_updates[_LLM_FILE_KEYS[env_key]] = clean
    if persist and file_updates:
        merged_llm: dict[str, str] = dict(get_llm_config())
        merged_llm.update(file_updates)
        save_config_file({"llm": merged_llm})
    return get_llm_config()
