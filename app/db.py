# SPDX-License-Identifier: GPL-3.0-or-later
"""SQLite database manager for doom-ed (DATA_DIR / 'doomed.sqlite').

Provides thread-safe connection handling, WAL mode, 0600 file permissions,
per-owner ACL checks (file_access), FTS5 course file search scoped by owner,
Fernet-encrypted page text storage (file_pages), and CRUD for threads,
messages, attachments, visuals, and quiz states.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import DATA_DIR, DOOMED_DB_PATH, ensure_data_dir
from app.security import Cipher


def _json_dumps_or_none(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return json.dumps(val, separators=(",", ":"), default=str)


def _fts_query(raw: str) -> str:
    """Build a safe FTS5 prefix query from user input."""
    tokens = re.findall(r"[A-Za-z0-9_]+", raw or "")
    if not tokens:
        return ""
    return " ".join(f'"{t}"*' for t in tokens[:12])


class Database:
    """Thread-safe SQLite database manager for doomed.sqlite."""

    def __init__(
        self,
        path: str | Path | None = None,
        cipher: Cipher | None = None,
    ) -> None:
        if path is None:
            ensure_data_dir(DATA_DIR)
            resolved_path = str(DOOMED_DB_PATH)
        else:
            resolved_path = str(path)
            if resolved_path != ":memory:":
                parent = Path(resolved_path).expanduser().resolve().parent
                parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        self.path: str = resolved_path
        self._memory: bool = self.path == ":memory:"
        self._cipher: Cipher = cipher if cipher is not None else Cipher()
        self._lock = threading.RLock()

        if not self._memory and not Path(self.path).exists():
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)

        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

        if not self._memory:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def _init_schema(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL;")
                self._conn.execute("PRAGMA foreign_keys=ON;")
            except Exception:
                pass

            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    sha256 TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    upstream_url TEXT,
                    filename TEXT NOT NULL,
                    mime TEXT,
                    size_bytes INTEGER,
                    page_count INTEGER,
                    blob_path TEXT NOT NULL,
                    encrypted INTEGER NOT NULL DEFAULT 1,
                    subject TEXT,
                    level TEXT,
                    rtype TEXT,
                    fetched_at REAL NOT NULL,
                    last_accessed REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS file_access (
                    owner TEXT NOT NULL,
                    ref TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    filename TEXT,
                    subject TEXT,
                    folder TEXT,
                    teacher TEXT,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (owner, ref)
                );

                CREATE INDEX IF NOT EXISTS idx_file_access_owner_sha256
                    ON file_access (owner, sha256);

                CREATE TABLE IF NOT EXISTS lms_files_meta (
                    owner TEXT NOT NULL,
                    ref TEXT NOT NULL,
                    course_id TEXT,
                    subject TEXT,
                    folder_path TEXT,
                    title TEXT,
                    filename TEXT,
                    teacher TEXT,
                    grade TEXT,
                    ext TEXT,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (owner, ref)
                );

                CREATE INDEX IF NOT EXISTS idx_lms_files_meta_owner
                    ON lms_files_meta (owner, subject);

                CREATE VIRTUAL TABLE IF NOT EXISTS lms_files_fts USING fts5(
                    owner UNINDEXED,
                    ref UNINDEXED,
                    subject,
                    folder_path,
                    title,
                    filename,
                    teacher
                );

                CREATE TABLE IF NOT EXISTS file_pages (
                    sha256 TEXT NOT NULL,
                    page_num INTEGER NOT NULL,
                    text_encrypted BLOB NOT NULL,
                    has_diagrams INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (sha256, page_num)
                );

                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    title TEXT NOT NULL,
                    topic TEXT,
                    subject TEXT,
                    phase TEXT NOT NULL DEFAULT 'probe',
                    dependency_map TEXT,
                    origin TEXT NOT NULL DEFAULT 'manual',
                    origin_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_threads_owner_updated
                    ON threads (owner, updated_at DESC);

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_calls_json TEXT,
                    tool_call_id TEXT,
                    cards_json TEXT,
                    citations_json TEXT,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_messages_thread_created
                    ON messages (thread_id, created_at ASC);

                CREATE TABLE IF NOT EXISTS thread_attachments (
                    thread_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    ref TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    filename TEXT,
                    attached_by TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (thread_id, ref)
                );

                CREATE TABLE IF NOT EXISTS visuals (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    brief TEXT,
                    source_code TEXT NOT NULL,
                    svg_data TEXT,
                    png_path TEXT,
                    verified INTEGER NOT NULL DEFAULT 0,
                    dom_warnings TEXT,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_visuals_thread
                    ON visuals (thread_id, created_at ASC);

                CREATE TABLE IF NOT EXISTS quiz_states (
                    call_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    question TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    correct_json TEXT NOT NULL,
                    explanation TEXT,
                    multi_select INTEGER NOT NULL DEFAULT 0,
                    answered INTEGER NOT NULL DEFAULT 0,
                    user_answer_json TEXT,
                    grade_result_json TEXT,
                    skill_ids_json TEXT,
                    item_id TEXT,
                    source TEXT NOT NULL DEFAULT 'lesson',
                    created_at REAL NOT NULL
                );

                -- --- Adaptive practice (additive, feature-flagged) ---------

                CREATE TABLE IF NOT EXISTS skills (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    level TEXT,
                    unit TEXT,
                    title TEXT NOT NULL,
                    description TEXT,
                    ib_code TEXT,
                    prereq_ids_json TEXT NOT NULL DEFAULT '[]',
                    exam_weight REAL NOT NULL DEFAULT 1.0,
                    source_kind TEXT,
                    source_ref TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_skills_subject
                    ON skills (subject, level);

                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY,
                    owner TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    skill_ids_json TEXT NOT NULL,
                    format TEXT NOT NULL,
                    stem TEXT NOT NULL,
                    options_json TEXT,
                    solution_enc BLOB NOT NULL,
                    difficulty REAL NOT NULL DEFAULT 0.0,
                    discrimination REAL NOT NULL DEFAULT 1.0,
                    guessing REAL NOT NULL DEFAULT 0.25,
                    source_kind TEXT NOT NULL,
                    source_ref TEXT,
                    source_sha256 TEXT,
                    verification TEXT NOT NULL DEFAULT 'draft',
                    model TEXT,
                    prompt_version TEXT,
                    content_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    retired_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_items_owner_verification
                    ON items (owner, verification);
                CREATE INDEX IF NOT EXISTS idx_items_content_hash
                    ON items (content_hash);

                CREATE TABLE IF NOT EXISTS item_stats (
                    item_id TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    correct INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS student_skill_state (
                    owner TEXT NOT NULL,
                    skill_id TEXT NOT NULL,
                    ability REAL NOT NULL DEFAULT 0.0,
                    mastery REAL NOT NULL DEFAULT 0.35,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    correct INTEGER NOT NULL DEFAULT 0,
                    unknowns INTEGER NOT NULL DEFAULT 0,
                    streak INTEGER NOT NULL DEFAULT 0,
                    stability REAL NOT NULL DEFAULT 1.0,
                    due_at REAL,
                    last_seen REAL,
                    checkpoint_eligible INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (owner, skill_id)
                );

                CREATE INDEX IF NOT EXISTS idx_skill_state_due
                    ON student_skill_state (owner, due_at);
                CREATE INDEX IF NOT EXISTS idx_skill_state_mastery
                    ON student_skill_state (owner, mastery);

                CREATE TABLE IF NOT EXISTS practice_attempts (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    attempt_token TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    item_version INTEGER NOT NULL,
                    skill_ids_json TEXT NOT NULL,
                    selected_json TEXT,
                    correct INTEGER NOT NULL DEFAULT 0,
                    dont_know INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER,
                    confidence INTEGER,
                    source TEXT NOT NULL,
                    thread_id TEXT,
                    card_id TEXT,
                    explanation_shown INTEGER NOT NULL DEFAULT 0,
                    planner_reason TEXT,
                    result_json TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE (owner, attempt_token)
                );

                CREATE INDEX IF NOT EXISTS idx_attempts_owner_created
                    ON practice_attempts (owner, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_attempts_owner_item
                    ON practice_attempts (owner, item_id);

                CREATE TABLE IF NOT EXISTS feed_plans (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    day TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    goal_cards INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE (owner, day)
                );

                CREATE TABLE IF NOT EXISTS feed_cards (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    item_id TEXT,
                    checkpoint_id TEXT,
                    skill_ids_json TEXT NOT NULL DEFAULT '[]',
                    reason_code TEXT,
                    reason_text TEXT,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempt_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE (plan_id, position)
                );

                CREATE INDEX IF NOT EXISTS idx_feed_cards_owner_state
                    ON feed_cards (owner, state);
                CREATE INDEX IF NOT EXISTS idx_feed_cards_plan_position
                    ON feed_cards (plan_id, position);

                CREATE TABLE IF NOT EXISTS feed_events (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    plan_id TEXT,
                    card_id TEXT,
                    event_type TEXT NOT NULL,
                    value_json TEXT,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_feed_events_owner_created
                    ON feed_events (owner, created_at DESC);

                CREATE TABLE IF NOT EXISTS checkpoint_runs (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    plan_id TEXT,
                    skill_ids_json TEXT NOT NULL,
                    item_ids_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'open',
                    score INTEGER,
                    total INTEGER,
                    time_limit_s INTEGER,
                    started_at REAL NOT NULL,
                    submitted_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_checkpoint_owner
                    ON checkpoint_runs (owner, started_at DESC);

                CREATE TABLE IF NOT EXISTS checkpoint_answers (
                    checkpoint_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    selected_json TEXT,
                    correct INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (checkpoint_id, item_id)
                );

                CREATE TABLE IF NOT EXISTS item_reports (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    note TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_item_reports_item
                    ON item_reports (item_id, status);

                CREATE TABLE IF NOT EXISTS owner_settings (
                    owner TEXT PRIMARY KEY,
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    daily_cards INTEGER NOT NULL DEFAULT 12,
                    target_retention REAL NOT NULL DEFAULT 0.90,
                    focus_subjects_json TEXT,
                    checkpoints_enabled INTEGER NOT NULL DEFAULT 1,
                    difficulty_pref TEXT NOT NULL DEFAULT 'balanced',
                    updated_at REAL NOT NULL
                );
                """
            )
            # Additive migrations for databases created before the practice
            # columns existed. SQLite has no ADD COLUMN IF NOT EXISTS.
            for table, column, ddl in (
                ("threads", "origin", "TEXT NOT NULL DEFAULT 'manual'"),
                ("threads", "origin_json", "TEXT"),
                ("quiz_states", "skill_ids_json", "TEXT"),
                ("quiz_states", "item_id", "TEXT"),
                ("quiz_states", "source", "TEXT NOT NULL DEFAULT 'lesson'"),
                ("practice_attempts", "result_json", "TEXT"),
            ):
                self._ensure_column(table, column, ddl)
            self._conn.commit()

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        """Add a column to an existing table once. Table/column names are internal."""
        with self._lock:
            columns = {
                str(row["name"])
                for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in columns:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                self._conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Hold the connection lock for a multi-statement transaction."""
        with self._lock:
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query_one(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

    def query_all(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Files & Per-Owner ACL (files, file_access)
    # ------------------------------------------------------------------

    def upsert_file(
        self,
        sha256: str,
        source_type: str,
        filename: str,
        blob_path: str,
        *,
        upstream_url: str | None = None,
        mime: str | None = None,
        size_bytes: int | None = None,
        page_count: int | None = None,
        encrypted: bool | int = 1,
        subject: str | None = None,
        level: str | None = None,
        rtype: str | None = None,
        fetched_at: float | None = None,
        last_accessed: float | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        f_at = float(fetched_at if fetched_at is not None else now)
        l_at = float(last_accessed if last_accessed is not None else now)
        enc_flag = 1 if encrypted else 0

        with self._lock:
            self._conn.execute(
                """INSERT INTO files (
                       sha256, source_type, upstream_url, filename, mime,
                       size_bytes, page_count, blob_path, encrypted,
                       subject, level, rtype, fetched_at, last_accessed
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(sha256) DO UPDATE SET
                       source_type = excluded.source_type,
                       upstream_url = COALESCE(excluded.upstream_url, files.upstream_url),
                       filename = excluded.filename,
                       mime = COALESCE(excluded.mime, files.mime),
                       size_bytes = COALESCE(excluded.size_bytes, files.size_bytes),
                       page_count = COALESCE(excluded.page_count, files.page_count),
                       blob_path = excluded.blob_path,
                       encrypted = excluded.encrypted,
                       subject = COALESCE(excluded.subject, files.subject),
                       level = COALESCE(excluded.level, files.level),
                       rtype = COALESCE(excluded.rtype, files.rtype),
                       last_accessed = excluded.last_accessed""",
                (
                    sha256,
                    source_type,
                    upstream_url,
                    filename,
                    mime,
                    size_bytes,
                    page_count,
                    blob_path,
                    enc_flag,
                    subject,
                    level,
                    rtype,
                    f_at,
                    l_at,
                ),
            )
            self._conn.commit()
        return self.get_file(sha256) or {}

    def get_file(self, sha256: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM files WHERE sha256 = ?", (sha256,))

    def touch_file(self, sha256: str) -> None:
        self.execute(
            "UPDATE files SET last_accessed = ? WHERE sha256 = ?",
            (time.time(), sha256),
        )

    def grant_file_access(
        self,
        owner: str,
        ref: str,
        sha256: str,
        *,
        filename: str | None = None,
        subject: str | None = None,
        folder: str | None = None,
        teacher: str | None = None,
    ) -> None:
        owner_clean = (owner or "").strip()
        ref_clean = (ref or "").strip()
        sha_clean = (sha256 or "").strip()
        if not owner_clean or not ref_clean or not sha_clean:
            raise ValueError("owner, ref, and sha256 are required for file_access")

        with self._lock:
            self._conn.execute(
                """INSERT INTO file_access (
                       owner, ref, sha256, filename, subject, folder, teacher, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(owner, ref) DO UPDATE SET
                       sha256 = excluded.sha256,
                       filename = COALESCE(excluded.filename, file_access.filename),
                       subject = COALESCE(excluded.subject, file_access.subject),
                       folder = COALESCE(excluded.folder, file_access.folder),
                       teacher = COALESCE(excluded.teacher, file_access.teacher),
                       updated_at = excluded.updated_at""",
                (
                    owner_clean,
                    ref_clean,
                    sha_clean,
                    filename,
                    subject,
                    folder,
                    teacher,
                    time.time(),
                ),
            )
            self._conn.commit()

    upsert_file_access = grant_file_access

    def can_access_file(self, owner: str, sha256: str) -> bool:
        """Return True if files.source_type == 'ib' OR a matching row exists in file_access for (owner, sha256)."""
        sha_clean = (sha256 or "").strip()
        if not sha_clean:
            return False

        with self._lock:
            file_row = self._conn.execute(
                "SELECT source_type FROM files WHERE sha256 = ?", (sha_clean,)
            ).fetchone()
            if file_row is not None and file_row["source_type"] == "ib":
                return True

            owner_clean = (owner or "").strip()
            if not owner_clean:
                return False

            acl_row = self._conn.execute(
                "SELECT 1 FROM file_access WHERE owner = ? AND sha256 = ? LIMIT 1",
                (owner_clean, sha_clean),
            ).fetchone()
            return acl_row is not None

    def get_file_access(self, owner: str, ref: str) -> dict[str, Any] | None:
        owner_clean = (owner or "").strip()
        ref_clean = (ref or "").strip()
        if not owner_clean or not ref_clean:
            return None
        return self.query_one(
            "SELECT * FROM file_access WHERE owner = ? AND ref = ?",
            (owner_clean, ref_clean),
        )

    def list_file_access(self, owner: str) -> list[dict[str, Any]]:
        owner_clean = (owner or "").strip()
        if not owner_clean:
            return []
        return self.query_all(
            "SELECT * FROM file_access WHERE owner = ? ORDER BY updated_at DESC",
            (owner_clean,),
        )

    # ------------------------------------------------------------------
    # LMS Course Files Metadata & FTS5 (lms_files_meta, lms_files_fts)
    # ------------------------------------------------------------------

    def upsert_lms_file_meta(
        self,
        owner: str,
        ref: str,
        *,
        course_id: str = "",
        subject: str = "",
        folder_path: str = "",
        title: str = "",
        filename: str = "",
        teacher: str = "",
        grade: str = "",
        ext: str = "",
    ) -> None:
        owner_clean = (owner or "").strip()
        ref_clean = (ref or "").strip()
        if not owner_clean or not ref_clean:
            raise ValueError("owner and ref are required for lms_files_meta")

        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO lms_files_meta (
                       owner, ref, course_id, subject, folder_path,
                       title, filename, teacher, grade, ext, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(owner, ref) DO UPDATE SET
                       course_id = excluded.course_id,
                       subject = excluded.subject,
                       folder_path = excluded.folder_path,
                       title = excluded.title,
                       filename = excluded.filename,
                       teacher = excluded.teacher,
                       grade = excluded.grade,
                       ext = excluded.ext,
                       updated_at = excluded.updated_at""",
                (
                    owner_clean,
                    ref_clean,
                    course_id,
                    subject,
                    folder_path,
                    title,
                    filename,
                    teacher,
                    grade,
                    ext,
                    now,
                ),
            )
            self._conn.execute(
                "DELETE FROM lms_files_fts WHERE owner = ? AND ref = ?",
                (owner_clean, ref_clean),
            )
            self._conn.execute(
                """INSERT INTO lms_files_fts (
                       owner, ref, subject, folder_path, title, filename, teacher
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    owner_clean,
                    ref_clean,
                    subject or "",
                    folder_path or "",
                    title or "",
                    filename or "",
                    teacher or "",
                ),
            )
            self._conn.commit()

    def upsert_lms_files_batch(self, owner: str, items: list[dict[str, Any]]) -> int:
        owner_clean = (owner or "").strip()
        if not owner_clean:
            raise ValueError("owner is required for upsert_lms_files_batch")
        count = 0
        now = time.time()
        with self._lock:
            for item in items:
                ref = str(item.get("ref") or "").strip()
                if not ref:
                    continue
                course_id = str(item.get("course_id") or "")
                subject = str(item.get("subject") or "")
                folder_path = str(item.get("folder_path") or item.get("folder") or "")
                title = str(item.get("title") or "")
                filename = str(item.get("filename") or item.get("name") or "")
                teacher = str(item.get("teacher") or "")
                grade = str(item.get("grade") or "")
                ext = str(item.get("ext") or "")

                self._conn.execute(
                    """INSERT INTO lms_files_meta (
                           owner, ref, course_id, subject, folder_path,
                           title, filename, teacher, grade, ext, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(owner, ref) DO UPDATE SET
                           course_id = excluded.course_id,
                           subject = excluded.subject,
                           folder_path = excluded.folder_path,
                           title = excluded.title,
                           filename = excluded.filename,
                           teacher = excluded.teacher,
                           grade = excluded.grade,
                           ext = excluded.ext,
                           updated_at = excluded.updated_at""",
                    (
                        owner_clean,
                        ref,
                        course_id,
                        subject,
                        folder_path,
                        title,
                        filename,
                        teacher,
                        grade,
                        ext,
                        now,
                    ),
                )
                self._conn.execute(
                    "DELETE FROM lms_files_fts WHERE owner = ? AND ref = ?",
                    (owner_clean, ref),
                )
                self._conn.execute(
                    """INSERT INTO lms_files_fts (
                           owner, ref, subject, folder_path, title, filename, teacher
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        owner_clean,
                        ref,
                        subject,
                        folder_path,
                        title,
                        filename,
                        teacher,
                    ),
                )
                count += 1
            self._conn.commit()
        return count

    def search_lms_files(
        self,
        owner: str,
        query: str = "",
        *,
        subject: str | None = None,
        folder: str | None = None,
        ext: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Search LMS course files strictly scoped to ``owner = ?``."""
        owner_clean = (owner or "").strip()
        if not owner_clean:
            raise ValueError("owner is required for search_lms_files")

        fts_q = _fts_query(query)
        params: list[Any] = []

        with self._lock:
            if fts_q:
                sql = """
                    SELECT m.*
                    FROM lms_files_fts f
                    JOIN lms_files_meta m ON m.owner = f.owner AND m.ref = f.ref
                    WHERE lms_files_fts MATCH ? AND f.owner = ? AND m.owner = ?
                """
                params = [fts_q, owner_clean, owner_clean]
            else:
                sql = "SELECT * FROM lms_files_meta m WHERE m.owner = ?"
                params = [owner_clean]

            if subject:
                sql += " AND LOWER(m.subject) LIKE ?"
                params.append(f"%{subject.strip().lower()}%")
            if folder:
                sql += " AND LOWER(m.folder_path) LIKE ?"
                params.append(f"%{folder.strip().lower()}%")
            if ext:
                clean_ext = ext.strip().lower().lstrip(".")
                sql += " AND LOWER(TRIM(m.ext, '.')) = ?"
                params.append(clean_ext)

            if fts_q:
                sql += " ORDER BY bm25(lms_files_fts) LIMIT ?"
            else:
                sql += " ORDER BY m.updated_at DESC LIMIT ?"
            params.append(int(limit))

            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    search_lms_files_fts = search_lms_files

    # ------------------------------------------------------------------
    # Encrypted File Pages (file_pages)
    # ------------------------------------------------------------------

    def put_file_page(
        self,
        sha256: str,
        page_num: int,
        text: str,
        has_diagrams: bool | int = 0,
    ) -> None:
        """Encrypt page text with Fernet and store in file_pages."""
        enc_blob = self._cipher.encrypt_bytes((text or "").encode("utf-8"))
        with self._lock:
            self._conn.execute(
                """INSERT INTO file_pages (sha256, page_num, text_encrypted, has_diagrams)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(sha256, page_num) DO UPDATE SET
                       text_encrypted = excluded.text_encrypted,
                       has_diagrams = excluded.has_diagrams""",
                (sha256, int(page_num), enc_blob, 1 if has_diagrams else 0),
            )
            self._conn.commit()

    def put_file_pages(
        self,
        sha256: str,
        pages: list[dict[str, Any] | tuple[int, str, bool | int]],
    ) -> None:
        with self._lock:
            for p in pages:
                if isinstance(p, dict):
                    pno = int(p["page_num"])
                    txt = str(p.get("text") or "")
                    diag = 1 if p.get("has_diagrams") else 0
                else:
                    pno, txt, diag = int(p[0]), str(p[1] or ""), (1 if p[2] else 0)
                enc_blob = self._cipher.encrypt_bytes(txt.encode("utf-8"))
                self._conn.execute(
                    """INSERT INTO file_pages (sha256, page_num, text_encrypted, has_diagrams)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(sha256, page_num) DO UPDATE SET
                           text_encrypted = excluded.text_encrypted,
                           has_diagrams = excluded.has_diagrams""",
                    (sha256, pno, enc_blob, diag),
                )
            self._conn.commit()

    def get_file_page(
        self, owner: str, sha256: str, page_num: int
    ) -> dict[str, Any] | None:
        """Read and decrypt a single file page after enforcing can_access_file(owner, sha256)."""
        if not self.can_access_file(owner, sha256):
            raise PermissionError(f"Owner '{owner}' is not authorized to access file {sha256}")

        with self._lock:
            row = self._conn.execute(
                "SELECT sha256, page_num, text_encrypted, has_diagrams FROM file_pages WHERE sha256 = ? AND page_num = ?",
                (sha256, int(page_num)),
            ).fetchone()
            if row is None:
                return None
            raw_bytes = self._cipher.decrypt_bytes(row["text_encrypted"])
            return {
                "sha256": row["sha256"],
                "page_num": int(row["page_num"]),
                "text": raw_bytes.decode("utf-8"),
                "has_diagrams": bool(row["has_diagrams"]),
            }

    def get_file_pages(
        self,
        owner: str,
        sha256: str,
        start_page: int = 1,
        end_page: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read and decrypt a range of file pages after enforcing can_access_file(owner, sha256)."""
        if not self.can_access_file(owner, sha256):
            raise PermissionError(f"Owner '{owner}' is not authorized to access file {sha256}")

        with self._lock:
            if end_page is not None:
                rows = self._conn.execute(
                    """SELECT sha256, page_num, text_encrypted, has_diagrams
                       FROM file_pages
                       WHERE sha256 = ? AND page_num >= ? AND page_num <= ?
                       ORDER BY page_num ASC""",
                    (sha256, int(start_page), int(end_page)),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """SELECT sha256, page_num, text_encrypted, has_diagrams
                       FROM file_pages
                       WHERE sha256 = ? AND page_num >= ?
                       ORDER BY page_num ASC""",
                    (sha256, int(start_page)),
                ).fetchall()

            result: list[dict[str, Any]] = []
            for row in rows:
                raw_bytes = self._cipher.decrypt_bytes(row["text_encrypted"])
                result.append(
                    {
                        "sha256": row["sha256"],
                        "page_num": int(row["page_num"]),
                        "text": raw_bytes.decode("utf-8"),
                        "has_diagrams": bool(row["has_diagrams"]),
                    }
                )
            return result

    # ------------------------------------------------------------------
    # Threads, Messages, Attachments
    # ------------------------------------------------------------------

    def create_thread(
        self,
        owner: str,
        title: str = "New Study Session",
        *,
        topic: str | None = None,
        subject: str | None = None,
        phase: str = "probe",
        dependency_map: str | dict[str, Any] | list[Any] | None = None,
        thread_id: str | None = None,
        origin: str = "manual",
        origin_json: str | dict[str, Any] | list[Any] | None = None,
    ) -> dict[str, Any]:
        owner_clean = (owner or "").strip()
        if not owner_clean:
            raise ValueError("owner is required to create a thread")
        tid = thread_id or uuid.uuid4().hex
        now = time.time()
        dep_json = _json_dumps_or_none(dependency_map)
        origin_payload = _json_dumps_or_none(origin_json)
        with self._lock:
            self._conn.execute(
                """INSERT INTO threads (
                       id, owner, title, topic, subject, phase, dependency_map,
                       origin, origin_json, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tid,
                    owner_clean,
                    title,
                    topic,
                    subject,
                    phase,
                    dep_json,
                    origin or "manual",
                    origin_payload,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return self.get_thread(owner_clean, tid) or {}

    def get_thread(self, owner: str, thread_id: str) -> dict[str, Any] | None:
        owner_clean = (owner or "").strip()
        if not owner_clean or not thread_id:
            return None
        return self.query_one(
            "SELECT * FROM threads WHERE id = ? AND owner = ?",
            (thread_id, owner_clean),
        )

    def list_threads(self, owner: str, limit: int = 50) -> list[dict[str, Any]]:
        owner_clean = (owner or "").strip()
        if not owner_clean:
            return []
        return self.query_all(
            "SELECT * FROM threads WHERE owner = ? ORDER BY updated_at DESC LIMIT ?",
            (owner_clean, int(limit)),
        )

    def update_thread(
        self,
        owner: str,
        thread_id: str,
        *,
        title: str | None = None,
        topic: str | None = None,
        subject: str | None = None,
        phase: str | None = None,
        dependency_map: str | dict[str, Any] | list[Any] | None = None,
    ) -> dict[str, Any] | None:
        existing = self.get_thread(owner, thread_id)
        if existing is None:
            return None
        now = time.time()
        new_title = title if title is not None else existing["title"]
        new_topic = topic if topic is not None else existing["topic"]
        new_subject = subject if subject is not None else existing["subject"]
        new_phase = phase if phase is not None else existing["phase"]
        new_dep = (
            _json_dumps_or_none(dependency_map)
            if dependency_map is not None
            else existing["dependency_map"]
        )
        with self._lock:
            self._conn.execute(
                """UPDATE threads
                   SET title = ?, topic = ?, subject = ?, phase = ?,
                       dependency_map = ?, updated_at = ?
                   WHERE id = ? AND owner = ?""",
                (
                    new_title,
                    new_topic,
                    new_subject,
                    new_phase,
                    new_dep,
                    now,
                    thread_id,
                    owner.strip(),
                ),
            )
            self._conn.commit()
        return self.get_thread(owner, thread_id)

    def delete_thread(self, owner: str, thread_id: str) -> bool:
        existing = self.get_thread(owner, thread_id)
        if existing is None:
            return False
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
            self._conn.execute(
                "DELETE FROM thread_attachments WHERE thread_id = ? AND owner = ?",
                (thread_id, owner.strip()),
            )
            self._conn.execute(
                "DELETE FROM visuals WHERE thread_id = ? AND owner = ?",
                (thread_id, owner.strip()),
            )
            self._conn.execute(
                "DELETE FROM quiz_states WHERE thread_id = ? AND owner = ?",
                (thread_id, owner.strip()),
            )
            self._conn.execute(
                "DELETE FROM threads WHERE id = ? AND owner = ?",
                (thread_id, owner.strip()),
            )
            self._conn.commit()
        return True

    def add_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        *,
        tool_calls: Any = None,
        tool_call_id: str | None = None,
        cards: Any = None,
        citations: Any = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        mid = message_id or uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO messages (
                       id, thread_id, role, content, tool_calls_json,
                       tool_call_id, cards_json, citations_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mid,
                    thread_id,
                    role,
                    content or "",
                    _json_dumps_or_none(tool_calls),
                    tool_call_id,
                    _json_dumps_or_none(cards),
                    _json_dumps_or_none(citations),
                    now,
                ),
            )
            self._conn.execute(
                "UPDATE threads SET updated_at = ? WHERE id = ?", (now, thread_id)
            )
            self._conn.commit()
        return self.query_one("SELECT * FROM messages WHERE id = ?", (mid,)) or {}

    def list_messages(
        self, thread_id: str, owner: str | None = None
    ) -> list[dict[str, Any]]:
        if owner is not None and self.get_thread(owner, thread_id) is None:
            return []
        return self.query_all(
            "SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at ASC",
            (thread_id,),
        )

    def attach_file_to_thread(
        self,
        thread_id: str,
        owner: str,
        ref: str,
        sha256: str,
        filename: str = "",
        attached_by: str = "user",
    ) -> dict[str, Any]:
        now = time.time()
        owner_clean = owner.strip()
        with self._lock:
            self._conn.execute(
                """INSERT INTO thread_attachments (
                       thread_id, owner, ref, sha256, filename, attached_by, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(thread_id, ref) DO UPDATE SET
                       sha256 = excluded.sha256,
                       filename = excluded.filename,
                       attached_by = excluded.attached_by,
                       created_at = excluded.created_at""",
                (thread_id, owner_clean, ref, sha256, filename, attached_by, now),
            )
            self._conn.commit()
        return (
            self.query_one(
                "SELECT * FROM thread_attachments WHERE thread_id = ? AND ref = ?",
                (thread_id, ref),
            )
            or {}
        )

    def list_thread_attachments(
        self, thread_id: str, owner: str | None = None
    ) -> list[dict[str, Any]]:
        if owner is not None:
            return self.query_all(
                "SELECT * FROM thread_attachments WHERE thread_id = ? AND owner = ? ORDER BY created_at ASC",
                (thread_id, owner.strip()),
            )
        return self.query_all(
            "SELECT * FROM thread_attachments WHERE thread_id = ? ORDER BY created_at ASC",
            (thread_id,),
        )

    # ------------------------------------------------------------------
    # Visuals (visuals)
    # ------------------------------------------------------------------

    def save_visual(
        self,
        thread_id: str,
        owner: str,
        kind: str,
        slug: str,
        source_code: str,
        *,
        brief: str | None = None,
        svg_data: str | None = None,
        png_path: str | None = None,
        verified: bool | int = 0,
        dom_warnings: Any = None,
        visual_id: str | None = None,
    ) -> dict[str, Any]:
        vid = visual_id or uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO visuals (
                       id, thread_id, owner, kind, slug, brief, source_code,
                       svg_data, png_path, verified, dom_warnings, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       source_code = excluded.source_code,
                       svg_data = COALESCE(excluded.svg_data, visuals.svg_data),
                       png_path = COALESCE(excluded.png_path, visuals.png_path),
                       verified = excluded.verified,
                       dom_warnings = excluded.dom_warnings""",
                (
                    vid,
                    thread_id,
                    owner.strip(),
                    kind,
                    slug,
                    brief,
                    source_code,
                    svg_data,
                    png_path,
                    1 if verified else 0,
                    _json_dumps_or_none(dom_warnings),
                    now,
                ),
            )
            self._conn.commit()
        return self.get_visual(vid) or {}

    def get_visual(
        self, visual_id: str, owner: str | None = None
    ) -> dict[str, Any] | None:
        if owner is not None:
            return self.query_one(
                "SELECT * FROM visuals WHERE id = ? AND owner = ?",
                (visual_id, owner.strip()),
            )
        return self.query_one("SELECT * FROM visuals WHERE id = ?", (visual_id,))

    def list_visuals(
        self, thread_id: str, owner: str | None = None
    ) -> list[dict[str, Any]]:
        if owner is not None:
            return self.query_all(
                "SELECT * FROM visuals WHERE thread_id = ? AND owner = ? ORDER BY created_at ASC",
                (thread_id, owner.strip()),
            )
        return self.query_all(
            "SELECT * FROM visuals WHERE thread_id = ? ORDER BY created_at ASC",
            (thread_id,),
        )

    # ------------------------------------------------------------------
    # Quiz States (quiz_states)
    # ------------------------------------------------------------------

    def create_quiz_state(
        self,
        call_id: str,
        thread_id: str,
        owner: str,
        question: str,
        options: list[Any] | str,
        correct: Any,
        *,
        explanation: str = "",
        multi_select: bool | int = 0,
        skill_ids: list[Any] | str | None = None,
        item_id: str | None = None,
        source: str = "lesson",
    ) -> dict[str, Any]:
        now = time.time()
        opts_json = _json_dumps_or_none(options) or "[]"
        corr_json = _json_dumps_or_none(correct) or "[]"
        skills_json = _json_dumps_or_none(skill_ids) or "[]"
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO quiz_states (
                       call_id, thread_id, owner, question, options_json,
                       correct_json, explanation, multi_select, answered,
                       user_answer_json, grade_result_json, skill_ids_json,
                       item_id, source, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?, ?)""",
                (
                    call_id,
                    thread_id,
                    owner.strip(),
                    question,
                    opts_json,
                    corr_json,
                    explanation,
                    1 if multi_select else 0,
                    skills_json,
                    item_id,
                    source or "lesson",
                    now,
                ),
            )
            self._conn.commit()
        return self.get_quiz_state(call_id, owner=owner) or {}

    def get_quiz_state(
        self, call_id: str, owner: str | None = None
    ) -> dict[str, Any] | None:
        if owner is not None:
            return self.query_one(
                "SELECT * FROM quiz_states WHERE call_id = ? AND owner = ?",
                (call_id, owner.strip()),
            )
        return self.query_one(
            "SELECT * FROM quiz_states WHERE call_id = ?", (call_id,)
        )

    def record_quiz_answer(
        self,
        call_id: str,
        owner: str,
        user_answer: Any,
        grade_result: Any,
    ) -> dict[str, Any] | None:
        state = self.get_quiz_state(call_id, owner=owner)
        if state is None:
            return None
        with self._lock:
            self._conn.execute(
                """UPDATE quiz_states
                   SET answered = 1, user_answer_json = ?, grade_result_json = ?
                   WHERE call_id = ? AND owner = ?""",
                (
                    _json_dumps_or_none(user_answer),
                    _json_dumps_or_none(grade_result),
                    call_id,
                    owner.strip(),
                ),
            )
            self._conn.commit()
        return self.get_quiz_state(call_id, owner=owner)

    def get_unpassed_quizzes(self, owner: str, limit: int = 10) -> list[dict[str, Any]]:
        """Return recent quiz states where the student missed or selected 'I don't know'."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT call_id, thread_id, question, options_json, grade_result_json, created_at
                   FROM quiz_states
                   WHERE owner = ? AND answered = 1
                   ORDER BY created_at DESC LIMIT 50""",
                (owner.strip(),),
            ).fetchall()
            unpassed = []
            for r in rows:
                row_dict = dict(r)
                res_raw = row_dict.get("grade_result_json")
                res = json.loads(res_raw) if res_raw else {}
                if res.get("correct") is False or res.get("dontKnow") is True:
                    unpassed.append(row_dict)
                    if len(unpassed) >= limit:
                        break
            return unpassed

    def close(self) -> None:
        with self._lock:
            self._conn.close()
