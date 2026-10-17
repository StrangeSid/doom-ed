# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent encrypted server-side sessions and L2 cache store for doom-ed."""

from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.config import DATA_DIR, INDUSLMS_TOKEN_FILE, SESSIONS_DB_PATH, ensure_data_dir
from app.security import Cipher, owner_key

DEFAULT_IDLE: float = 12 * 3600.0


def _extract_jwt_uid(token: str) -> str:
    """Best-effort extraction of user_id/sub from an unverified JWT payload."""
    if not token or token.count(".") != 2:
        return ""
    try:
        payload_b64 = token.split(".")[1]
        padding = "=" * (-len(payload_b64) % 4)
        raw = base64.urlsafe_b64decode(payload_b64 + padding)
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            uid = data.get("user_id") or data.get("sub") or data.get("id") or ""
            return str(uid).strip()
    except Exception:
        pass
    return ""


class Session:
    """In-memory representation of an authenticated student session."""

    def __init__(
        self,
        access: str = "",
        refresh: str = "",
        tenant: str = "",
        user: dict[str, Any] | None = None,
        *,
        sid: str | None = None,
        name: str = "",
        email: str = "",
        uid: str = "",
        grade: str = "",
        seen: float | None = None,
        created: float | None = None,
    ) -> None:
        user_dict = user if isinstance(user, dict) else {}
        resolved_tenant = (tenant or "").strip()
        if not resolved_tenant:
            resolved_tenant = str(
                user_dict.get("tenant") or user_dict.get("tenant_id") or ""
            ).strip()
        if not resolved_tenant and isinstance(user_dict.get("roles"), list):
            for role in user_dict["roles"]:
                if isinstance(role, dict) and role.get("tenant_id"):
                    resolved_tenant = str(role["tenant_id"]).strip()
                    break

        resolved_email = (email or user_dict.get("email") or "").strip()
        resolved_name = (
            name
            or user_dict.get("full_name")
            or user_dict.get("name")
            or resolved_email
            or ""
        ).strip()
        resolved_uid = (
            uid
            or str(
                user_dict.get("id")
                or user_dict.get("uid")
                or user_dict.get("user_id")
                or ""
            )
        ).strip()
        if not resolved_uid:
            resolved_uid = _extract_jwt_uid(access)
        resolved_grade = (
            grade
            or str(
                user_dict.get("grade")
                or user_dict.get("grade_name")
                or user_dict.get("class_name")
                or ""
            )
        ).strip()

        now = time.time()
        self.sid: str | None = sid
        self.access: str = access or ""
        self.refresh: str = refresh or ""
        self.tenant: str = resolved_tenant
        self.name: str = resolved_name
        self.email: str = resolved_email
        self.uid: str = resolved_uid
        self.grade: str = resolved_grade
        self.seen: float = float(seen if seen is not None else now)
        self.created: float = float(created if created is not None else self.seen)
        self.lock = threading.Lock()

    @property
    def owner(self) -> str:
        """Canonical per-student isolation key '{tenant}:{uid}'."""
        return owner_key(self.tenant, self.uid)


class SessionStore:
    """SQLite-backed session and encrypted L2 cache store (0600 permissions)."""

    def __init__(
        self,
        path: str | Path | None = None,
        idle: float = DEFAULT_IDLE,
        cipher: Cipher | None = None,
    ) -> None:
        if path is None:
            ensure_data_dir(DATA_DIR)
            resolved_path = str(SESSIONS_DB_PATH)
        else:
            resolved_path = str(path)
            if resolved_path != ":memory:":
                parent = Path(resolved_path).expanduser().resolve().parent
                parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        self.path: str = resolved_path
        self.idle: float = float(idle)
        self._cipher: Cipher = cipher if cipher is not None else Cipher()
        self._lock = threading.Lock()
        self._memory: bool = self.path == ":memory:"

        if not self._memory and not Path(self.path).exists():
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)

        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=10.0)
        try:
            self._db.execute("PRAGMA journal_mode=WAL;")
        except Exception:
            pass

        self._db.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                sid TEXT PRIMARY KEY,
                access TEXT NOT NULL,
                refresh TEXT NOT NULL,
                tenant TEXT NOT NULL,
                name TEXT,
                email TEXT,
                uid TEXT NOT NULL,
                grade TEXT,
                seen REAL NOT NULL,
                created REAL NOT NULL
            )"""
        )
        cols = {row[1] for row in self._db.execute("PRAGMA table_info(sessions)")}
        if "uid" not in cols:
            self._db.execute("ALTER TABLE sessions ADD COLUMN uid TEXT DEFAULT ''")
        if "grade" not in cols:
            self._db.execute("ALTER TABLE sessions ADD COLUMN grade TEXT DEFAULT ''")

        self._db.execute(
            """CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        # Drop any legacy cache keys not strictly scoped as {prefix}:{tenant}:{uid}
        self._db.execute(
            "DELETE FROM cache WHERE (key LIKE 'files:%' OR key LIKE 'payload:%') AND key NOT LIKE '%:%:%'"
        )
        self._db.commit()
        self.purge()

        if not self._memory:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def _enc(self, value: str) -> str:
        return self._cipher.encrypt_str(value or "")

    def _dec(self, token: str) -> str | None:
        if not token:
            return ""
        try:
            return self._cipher.decrypt_str(token)
        except Exception:
            return None

    def purge(self) -> None:
        """Delete expired sessions and L2 cache rows older than 24 hours."""
        now = time.time()
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE seen < ?", (now - self.idle,))
            self._db.execute("DELETE FROM cache WHERE updated_at < ?", (now - 86400.0,))
            self._db.commit()

    def add(self, s: Session) -> str:
        """Persist a new Session with a fresh random sid and encrypted tokens."""
        sid = s.sid or secrets.token_urlsafe(32)
        s.sid = sid
        s.seen = time.time()
        if not s.created:
            s.created = s.seen
        with self._lock:
            self._db.execute(
                """INSERT OR REPLACE INTO sessions
                   (sid, access, refresh, tenant, name, email, uid, grade, seen, created)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sid,
                    self._enc(s.access),
                    self._enc(s.refresh),
                    s.tenant,
                    s.name,
                    s.email,
                    s.uid,
                    s.grade,
                    s.seen,
                    s.created,
                ),
            )
            self._db.commit()
        return sid

    def get(self, sid: str | None) -> Session | None:
        """Look up a session by sid, decrypting tokens and updating seen timestamp."""
        if not sid:
            return None
        now = time.time()
        with self._lock:
            row = self._db.execute(
                """SELECT sid, access, refresh, tenant, name, email, uid, grade, seen, created
                   FROM sessions WHERE sid = ?""",
                (sid,),
            ).fetchone()
            if not row:
                return None
            (
                row_sid,
                enc_access,
                enc_refresh,
                tenant,
                name,
                email,
                uid,
                grade,
                seen,
                created,
            ) = row
            if now - float(seen) > self.idle:
                self._db.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
                self._db.commit()
                return None

            access = self._dec(enc_access)
            refresh = self._dec(enc_refresh)
            if access is None or refresh is None:
                # Undecryptable (e.g. rotated secret key): drop row and treat as expired
                self._db.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
                self._db.commit()
                return None

            self._db.execute("UPDATE sessions SET seen = ? WHERE sid = ?", (now, sid))
            self._db.commit()

        return Session(
            access=access,
            refresh=refresh,
            tenant=tenant or "",
            sid=row_sid,
            name=name or "",
            email=email or "",
            uid=uid or "",
            grade=grade or "",
            seen=now,
            created=float(created),
        )

    def save(self, s: Session) -> None:
        """Persist refreshed tokens and metadata for an existing Session."""
        if not s.sid:
            return
        s.seen = time.time()
        with self._lock:
            self._db.execute(
                """UPDATE sessions
                   SET access = ?, refresh = ?, tenant = ?, name = ?, email = ?,
                       uid = ?, grade = ?, seen = ?
                   WHERE sid = ?""",
                (
                    self._enc(s.access),
                    self._enc(s.refresh),
                    s.tenant,
                    s.name,
                    s.email,
                    s.uid,
                    s.grade,
                    s.seen,
                    s.sid,
                ),
            )
            self._db.commit()

    def drop(self, sid: str | None) -> None:
        """Delete a session row (logout/revocation)."""
        if not sid:
            return
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
            self._db.commit()

    def count(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) FROM sessions").fetchone()
            return int(row[0]) if row else 0

    def set_cache(self, key: str, value_json: str) -> None:
        """Store an L2 cache entry encrypted with Fernet at rest."""
        if not key:
            raise ValueError("Cache key must be non-empty")
        enc_val = self._enc(value_json)
        with self._lock:
            self._db.execute(
                """INSERT INTO cache (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = excluded.updated_at""",
                (key, enc_val, time.time()),
            )
            self._db.commit()

    def get_cache_with_timestamp(self, key: str) -> tuple[str, float] | None:
        """Return (decrypted_json, updated_at) for an L2 cache key, or None."""
        if not key:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT value, updated_at FROM cache WHERE key = ?", (key,)
            ).fetchone()
            if not row:
                return None
            enc_val, updated_at = row
            dec_val = self._dec(enc_val)
            if dec_val is None:
                self._db.execute("DELETE FROM cache WHERE key = ?", (key,))
                self._db.commit()
                return None
            return dec_val, float(updated_at)

    def get_cache(self, key: str, max_age: float | None = None) -> str | None:
        """Return decrypted L2 cached JSON if present and within max_age seconds."""
        res = self.get_cache_with_timestamp(key)
        if res is None:
            return None
        dec_val, updated_at = res
        if max_age is not None and (time.time() - updated_at) > float(max_age):
            return None
        return dec_val

    def delete_cache_prefix(self, prefix: str) -> None:
        """Delete all L2 cache rows matching the given key prefix."""
        if not prefix:
            return
        with self._lock:
            self._db.execute("DELETE FROM cache WHERE key LIKE ?", (f"{prefix}%",))
            self._db.commit()

    # Compatibility aliases with openlms.sessions.Store
    cache_put = set_cache
    cache_get = get_cache_with_timestamp
    cache_drop_prefix = delete_cache_prefix

    def close(self) -> None:
        with self._lock:
            self._db.close()


Store = SessionStore


def import_local_indus_token(
    store: SessionStore, token_path: Path | str | None = None
) -> Session | None:
    """Bootstrap an encrypted Session from ~/.induslms_token.json if present and valid."""
    target = (
        Path(token_path).expanduser()
        if token_path is not None
        else INDUSLMS_TOKEN_FILE
    )
    if not target.exists() or not target.is_file():
        return None

    try:
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    access = str(data.get("access") or data.get("access_token") or "").strip()
    refresh = str(data.get("refresh") or data.get("refresh_token") or "").strip()
    if not access:
        return None

    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    tenant = str(data.get("tenant") or data.get("tenant_id") or "").strip()
    if not tenant and isinstance(user.get("roles"), list):
        for role in user["roles"]:
            if isinstance(role, dict) and role.get("tenant_id"):
                tenant = str(role["tenant_id"]).strip()
                break

    if not tenant:
        return None

    session = Session(access=access, refresh=refresh, tenant=tenant, user=user)
    if not session.uid:
        return None

    store.add(session)
    return session
