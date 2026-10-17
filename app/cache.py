# SPDX-License-Identifier: GPL-3.0-or-later
"""Two-tier (L1 in-memory + L2 Fernet-encrypted SQLite) per-student cache for doom-ed.

L2 keys are strictly scoped to ``payload:{tenant}:{uid}`` and ``files:{tenant}:{uid}``
so students on the same school tenant never share cached LMS data or file trees.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from app.config import (
    CACHE_SIZE,
    DATA_TTL,
    DETAIL_TTL,
    FILES_PERSIST_TTL,
    FILES_TTL,
    PAYLOAD_PERSIST_TTL,
)
from app.security import owner_key

_store = None


def set_store(store: Any) -> None:
    """Bind the SessionStore instance used for L2 encrypted persistence."""
    global _store
    _store = store


def get_store() -> Any:
    """Return the bound SessionStore instance, if any."""
    global _store
    if _store is not None:
        return _store
    for mod_name in ("app.main", "app.server", "app.app"):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "sessions"):
            return getattr(mod, "sessions")
    return None


class _Entry:
    """L1 in-memory cache entry for a single student session."""

    def __init__(
        self,
        tenant: str = "",
        uid: str = "",
        sid: str = "",
    ) -> None:
        self.payload: dict[str, Any] | None = None
        self.built_at: float = 0.0
        self.etag: str | None = None
        self.files: list[dict[str, Any]] | None = None
        self.files_at: float = 0.0
        self.lock = threading.Lock()
        self.crawl_lock = threading.Lock()
        self.crawling: bool = False
        self.crawl_done = threading.Event()
        self.crawl_done.set()
        self.revalidate_lock = threading.Lock()
        self.revalidating: bool = False
        self.revalidate_done = threading.Event()
        self.revalidate_done.set()
        self.detail: dict[str, tuple[float, Any]] = {}
        self.memo: dict[str, tuple[float, Any]] = self.detail
        self.memo_lock = threading.Lock()
        self.tenant: str = (tenant or "").strip()
        self.uid: str = (uid or "").strip()
        self.sid: str = (sid or "").strip()


_entries: OrderedDict[str, _Entry] = OrderedDict()
_entries_lock = threading.Lock()


def _evict_if_needed() -> None:
    while len(_entries) > CACHE_SIZE:
        _entries.popitem(last=False)


def _resolve_owner(entry: _Entry, tenant: str = "", uid: str = "") -> str | None:
    """Return 'tenant:uid' strictly when both tenant and uid are non-empty."""
    t = (tenant or entry.tenant or "").strip()
    u = (uid or entry.uid or "").strip()
    if not t or not u:
        return None
    try:
        return owner_key(t, u)
    except ValueError:
        return None


def _payload_key(entry: _Entry, tenant: str = "", uid: str = "") -> str | None:
    owner = _resolve_owner(entry, tenant=tenant, uid=uid)
    return f"payload:{owner}" if owner else None


def _files_key(entry: _Entry, tenant: str = "", uid: str = "") -> str | None:
    owner = _resolve_owner(entry, tenant=tenant, uid=uid)
    return f"files:{owner}" if owner else None


def etag_for(payload: dict[str, Any]) -> str:
    """Compute a weak HTTP ETag for a payload dict."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f'W/"{digest}"'


def get_entry(
    sid: str | _Entry | None,
    tenant: str = "",
    uid: str = "",
) -> _Entry | None:
    """Return or create the L1 _Entry for sid, enforcing owner consistency."""
    if isinstance(sid, _Entry):
        if tenant and not sid.tenant:
            sid.tenant = tenant.strip()
        if uid and not sid.uid:
            sid.uid = uid.strip()
        return sid
    if not sid:
        return None

    t = (tenant or "").strip()
    u = (uid or "").strip()
    with _entries_lock:
        entry = _entries.get(sid)
        if entry is not None:
            # If sid was ever reassigned to a different tenant or uid, replace entry
            if (t and entry.tenant and entry.tenant != t) or (
                u and entry.uid and entry.uid != u
            ):
                entry = _Entry(tenant=t, uid=u, sid=sid)
                _entries[sid] = entry
            else:
                if t and not entry.tenant:
                    entry.tenant = t
                if u and not entry.uid:
                    entry.uid = u
                if not entry.sid:
                    entry.sid = sid
                _entries.move_to_end(sid)
        else:
            entry = _Entry(tenant=t, uid=u, sid=sid)
            _entries[sid] = entry
            _evict_if_needed()
        return entry


def get_payload(
    entry: _Entry,
    refresh: bool = False,
    allow_stale: bool = False,
    tenant: str = "",
    uid: str = "",
) -> dict[str, Any] | None:
    """Return fresh cached payload from L1 or L2, or None when missing/stale/forced."""
    if refresh:
        return None
    if tenant and not entry.tenant:
        entry.tenant = tenant.strip()
    if uid and not entry.uid:
        entry.uid = uid.strip()

    now = time.time()
    if entry.payload is not None:
        age = now - entry.built_at
        if age <= DATA_TTL:
            return entry.payload
        if allow_stale and age <= PAYLOAD_PERSIST_TTL:
            return entry.payload
        if not allow_stale:
            return None

    key = _payload_key(entry, tenant=tenant, uid=uid)
    if key:
        store = get_store()
        if store is not None:
            max_ttl = PAYLOAD_PERSIST_TTL if allow_stale else DATA_TTL
            if hasattr(store, "get_cache_with_timestamp"):
                row = store.get_cache_with_timestamp(key)
                if row is not None:
                    raw_json, updated_at = row
                    if (now - updated_at) <= max_ttl:
                        try:
                            payload = json.loads(raw_json)
                            entry.payload = payload
                            entry.built_at = updated_at
                            entry.etag = etag_for(payload)
                            return payload
                        except Exception:
                            pass
            elif hasattr(store, "get_cache"):
                raw_json = store.get_cache(key, max_age=max_ttl)
                if raw_json is not None:
                    try:
                        payload = json.loads(raw_json)
                        entry.payload = payload
                        entry.built_at = now
                        entry.etag = etag_for(payload)
                        return payload
                    except Exception:
                        pass
    return None


def put_payload(
    entry: _Entry,
    payload: dict[str, Any],
    tenant: str = "",
    uid: str = "",
) -> str:
    """Store payload in L1 and encrypted L2 ('payload:{tenant}:{uid}'). Returns ETag."""
    if tenant and not entry.tenant:
        entry.tenant = tenant.strip()
    if uid and not entry.uid:
        entry.uid = uid.strip()

    entry.payload = payload
    entry.built_at = time.time()
    entry.etag = etag_for(payload)

    key = _payload_key(entry, tenant=tenant, uid=uid)
    if key:
        store = get_store()
        if store is not None:
            try:
                raw_json = json.dumps(payload, separators=(",", ":"), default=str)
                store.set_cache(key, raw_json)
            except Exception:
                pass
    return entry.etag


def get_files(
    sid_or_entry: str | _Entry,
    allow_stale: bool = False,
    tenant: str = "",
    uid: str = "",
) -> list[dict[str, Any]] | None:
    """Return fresh cached files list from L1 or L2 ('files:{tenant}:{uid}'), or None."""
    entry = get_entry(sid_or_entry, tenant=tenant, uid=uid)
    if entry is None:
        return None

    now = time.time()
    if entry.files is not None:
        age = now - entry.files_at
        if age <= FILES_TTL:
            return entry.files
        if allow_stale and age <= FILES_PERSIST_TTL:
            return entry.files
        if not allow_stale:
            return None

    key = _files_key(entry, tenant=tenant, uid=uid)
    if key:
        store = get_store()
        if store is not None:
            max_ttl = FILES_PERSIST_TTL if allow_stale else FILES_TTL
            if hasattr(store, "get_cache_with_timestamp"):
                row = store.get_cache_with_timestamp(key)
                if row is not None:
                    raw_json, updated_at = row
                    if (now - updated_at) <= max_ttl:
                        try:
                            files = json.loads(raw_json)
                            entry.files = files
                            entry.files_at = updated_at
                            return files
                        except Exception:
                            pass
            elif hasattr(store, "get_cache"):
                raw_json = store.get_cache(key, max_age=max_ttl)
                if raw_json is not None:
                    try:
                        files = json.loads(raw_json)
                        entry.files = files
                        entry.files_at = now
                        return files
                    except Exception:
                        pass
    return None


def put_files(
    sid: str | _Entry,
    files: list[dict[str, Any]],
    tenant: str = "",
    uid: str = "",
) -> None:
    """Store crawled files list in L1 and encrypted L2 ('files:{tenant}:{uid}')."""
    entry = get_entry(sid, tenant=tenant, uid=uid)
    if entry is None:
        return

    entry.files = files
    entry.files_at = time.time()
    if entry.payload is not None:
        entry.payload = {
            **entry.payload,
            "resources": files,
            "resourcesPending": False,
            "resourcesStale": False,
        }
        put_payload(entry, entry.payload, tenant=tenant, uid=uid)

    key = _files_key(entry, tenant=tenant, uid=uid)
    if key:
        store = get_store()
        if store is not None:
            try:
                raw_json = json.dumps(files, separators=(",", ":"), default=str)
                store.set_cache(key, raw_json)
            except Exception:
                pass


def get_or_build(
    sid: str,
    builder: Callable[[list[dict[str, Any]] | None], dict[str, Any]],
    refresh: bool = False,
    tenant: str = "",
    uid: str = "",
) -> tuple[dict[str, Any], str]:
    """Return (payload, etag) for sid, building once under singleflight per-sid lock."""
    entry = get_entry(sid, tenant=tenant, uid=uid)
    if entry is None:
        payload = builder(None)
        return payload, etag_for(payload)

    with entry.lock:
        cached = get_payload(entry, refresh=refresh, tenant=tenant, uid=uid)
        if cached is not None and entry.etag is not None:
            return cached, entry.etag
        files = get_files(entry, allow_stale=True, tenant=tenant, uid=uid)
        payload = builder(files)
        etag = put_payload(entry, payload, tenant=tenant, uid=uid)
        return payload, etag


def memo(
    entry: _Entry | None,
    key: str,
    fn: Callable[[], Any],
    ttl: float | None = None,
    refresh: bool = False,
) -> Any:
    """Cache detail responses (graded EOL results, FA details, triage) on entry."""
    if entry is None:
        return fn()
    effective_ttl = float(DETAIL_TTL if ttl is None else ttl)
    now = time.time()
    if not refresh:
        with entry.memo_lock:
            hit = entry.detail.get(key)
            if hit is not None and (now - hit[0]) < effective_ttl:
                return hit[1]
    val = fn()
    with entry.memo_lock:
        entry.detail[key] = (time.time(), val)
    return val


def invalidate(entry: _Entry | None, *prefixes: str) -> None:
    """Drop L1 payload + matching detail keys and L2 persisted payload after an LMS write."""
    if entry is None:
        return
    with entry.lock:
        entry.payload = None
        entry.built_at = 0.0
        entry.etag = None
        key = _payload_key(entry)
        if key:
            store = get_store()
            if store is not None:
                store.delete_cache_prefix(key)
    with entry.memo_lock:
        if not prefixes:
            entry.detail.clear()
        else:
            drop_keys = [
                k for k in entry.detail if any(k.startswith(p) for p in prefixes)
            ]
            for k in drop_keys:
                entry.detail.pop(k, None)


def drop(sid: str | None) -> None:
    """Evict L1 entry for sid (e.g. on logout)."""
    if not sid:
        return
    with _entries_lock:
        _entries.pop(sid, None)


def clear() -> None:
    """Clear all L1 entries (used in tests)."""
    with _entries_lock:
        _entries.clear()
