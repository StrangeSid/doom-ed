# SPDX-License-Identifier: GPL-3.0-or-later
"""Security primitives for doom-ed: Fernet encryption at rest, SSRF guard,
per-student owner keys, single-use write confirmation tokens, and live-test lockout.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import threading
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from cryptography.fernet import Fernet

from app.config import ALLOW_LOCAL_IB, SECRET_KEY, derive_fernet_key

LIVE_TEST_ERROR_MSG = "Live test protection: assistant only explains graded/submitted work."


class SSRFError(ValueError, PermissionError):
    """Raised when an outbound URL fails SSRF validation."""


class _DualMethod:
    """Descriptor allowing Cipher methods to be called on either an instance or the class."""

    def __init__(self, fn):
        self.fn = fn

    def __get__(self, instance, owner=None):
        if instance is None:
            instance = owner._default_instance()
        return self.fn.__get__(instance, owner)


class Cipher:
    """Fernet encryption wrapper using DOOMED_SECRET_KEY (always active)."""

    _default: Cipher | None = None
    _lock = threading.Lock()

    def __init__(self, secret: str | bytes | None = None) -> None:
        key = derive_fernet_key(secret) if secret is not None else SECRET_KEY
        self._key: bytes = key
        self._fernet = Fernet(key)

    @classmethod
    def _default_instance(cls) -> Cipher:
        if cls._default is None:
            with cls._lock:
                if cls._default is None:
                    cls._default = cls(SECRET_KEY)
        return cls._default

    def _encrypt_bytes(self, data: bytes) -> bytes:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("encrypt_bytes expects bytes-like data")
        return self._fernet.encrypt(bytes(data))

    def _decrypt_bytes(self, token: bytes) -> bytes:
        if isinstance(token, str):
            token = token.encode("ascii")
        elif not isinstance(token, (bytes, bytearray, memoryview)):
            raise TypeError("decrypt_bytes expects bytes or ascii str token")
        return self._fernet.decrypt(bytes(token))

    def _encrypt_str(self, s: str) -> str:
        if not isinstance(s, str):
            raise TypeError("encrypt_str expects a str")
        return self._fernet.encrypt(s.encode("utf-8")).decode("ascii")

    def _decrypt_str(self, token: str) -> str:
        if isinstance(token, bytes):
            raw = token
        elif isinstance(token, str):
            raw = token.encode("ascii")
        else:
            raise TypeError("decrypt_str expects a str or bytes token")
        return self._fernet.decrypt(raw).decode("utf-8")

    encrypt_bytes = _DualMethod(_encrypt_bytes)
    decrypt_bytes = _DualMethod(_decrypt_bytes)
    encrypt_str = _DualMethod(_encrypt_str)
    decrypt_str = _DualMethod(_decrypt_str)


def owner_key(tenant: str, uid: str) -> str:
    """Return canonical per-student isolation key '{tenant}:{uid}'.

    Raises ValueError if either tenant or uid is missing/empty.
    """
    t = (tenant or "").strip() if isinstance(tenant, str) else ""
    u = (uid or "").strip() if isinstance(uid, str) else ""
    if not t or not u:
        raise ValueError("Both tenant and uid must be non-empty strings")
    return f"{t}:{u}"


_BLOCKED_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::/128"),
)


def _check_ip_allowed(ip_str: str) -> None:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError as exc:
        raise SSRFError(f"Invalid IP address: {ip_str}") from exc

    # Unwrap IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1, ::ffff:169.254.169.254)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if str(ip) == "169.254.169.254":
        raise SSRFError("Access to cloud metadata IP 169.254.169.254 is forbidden")

    for net in _BLOCKED_NETWORKS:
        if ip in net:
            raise SSRFError(f"Access to blocked network IP {ip} is forbidden")

    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or not ip.is_global
    ):
        raise SSRFError(f"Access to non-global/private IP {ip} is forbidden")


def validate_fetch_url(url: str, *, allow_local_ib: bool | None = None) -> None:
    """Validate an outbound URL against SSRF rules.

    Enforces HTTPS on port 443 (except localhost:8471 when allow_local_ib=True),
    rejects userinfo, resolves DNS with socket.getaddrinfo, and blocks loopback,
    private, link-local, multicast, unspecified, and cloud metadata IPs.
    """
    if not url or not isinstance(url, str):
        raise SSRFError("Missing or invalid URL")

    try:
        parsed = urlparse(url.strip())
    except Exception as exc:
        raise SSRFError("Malformed URL") from exc

    scheme = (parsed.scheme or "").lower()
    if not parsed.hostname:
        raise SSRFError("Missing hostname in URL")

    if parsed.username is not None or parsed.password is not None:
        raise SSRFError("Userinfo is not allowed in URL")

    hostname = parsed.hostname.strip("[]").lower()
    local_ib_ok = ALLOW_LOCAL_IB if allow_local_ib is None else allow_local_ib

    # Explicit opt-in exception for local IB sidecar on localhost:8471
    if (
        local_ib_ok
        and scheme in ("http", "https")
        and hostname in ("localhost", "127.0.0.1")
        and parsed.port == 8471
    ):
        return

    if scheme != "https":
        raise SSRFError("Only HTTPS URLs are supported")

    port = parsed.port if parsed.port is not None else 443
    if port != 443:
        raise SSRFError("Only port 443 is supported")

    if hostname in ("localhost", "localhost.localdomain") or hostname.endswith(".localhost"):
        raise SSRFError("Access to localhost is forbidden")

    # If hostname is already an IP literal, check it immediately before DNS lookup
    try:
        ipaddress.ip_address(hostname)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False

    if is_ip_literal:
        _check_ip_allowed(hostname)

    try:
        addrs = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SSRFError(f"Could not resolve hostname: {hostname}") from exc
    except Exception as exc:
        raise SSRFError(f"Host resolution error for {hostname}") from exc

    if not addrs:
        raise SSRFError(f"No IP address found for hostname: {hostname}")

    for _, _, _, _, sockaddr in addrs:
        ip_str = sockaddr[0]
        # Strip IPv6 scope ID if present (e.g. fe80::1%lo0)
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        _check_ip_allowed(ip_str)


def safe_fetch(
    url: str,
    headers: dict[str, str] | None = None,
    stream: bool = True,
    max_redirects: int = 5,
    timeout: int = 30,
    allow_local_ib: bool | None = None,
    session: requests.Session | None = None,
) -> requests.Response:
    """Perform an HTTP GET while validating the initial URL and every redirect hop against SSRF."""
    current_url = url
    http = session if session is not None else requests
    req_headers = dict(headers) if headers else {}

    for _ in range(max_redirects + 1):
        validate_fetch_url(current_url, allow_local_ib=allow_local_ib)
        resp = http.get(
            current_url,
            headers=req_headers,
            stream=stream,
            allow_redirects=False,
            timeout=timeout,
        )
        if 300 <= resp.status_code < 400 and "location" in resp.headers:
            location = resp.headers["location"]
            current_url = urljoin(current_url, location)
            if hasattr(resp, "close"):
                resp.close()
            continue
        return resp

    raise SSRFError(f"Too many redirects (exceeded {max_redirects})")


def sanitize_filename(name: str) -> str:
    """Sanitize a filename by stripping control characters, slashes, quotes, and path traversal."""
    if not name or not isinstance(name, str):
        return "file"
    # Normalize slashes and take basename
    cleaned = name.replace("\\", "/")
    parts = [p for p in cleaned.split("/") if p and p not in (".", "..")]
    base = parts[-1] if parts else ""
    # Strip control chars, slashes, quotes, semicolons, and path-unsafe characters
    base = re.sub(r'[\x00-\x1f\x7f/\\"\'`<>|:*?;]', "", base)
    # Remove any remaining '..' traversal sequences
    while ".." in base:
        base = base.replace("..", "")
    base = base.strip(" .")[:200]
    return base or "file"


class WriteConfirmationManager:
    """Single-use, time-limited (TTL=600s), owner-bound tokens for LMS write actions."""

    DEFAULT_TTL = 600.0

    def __init__(self, ttl: float = DEFAULT_TTL, secret: bytes | str | None = None) -> None:
        self.ttl = float(ttl)
        self._key = derive_fernet_key(secret) if secret is not None else SECRET_KEY
        self._lock = threading.Lock()
        self._tokens: dict[str, dict[str, Any]] = {}

    def _sign(
        self, nonce: str, owner: str, action: str, payload_hash: str, expires_at: float
    ) -> str:
        msg = f"{nonce}|{owner}|{action}|{payload_hash}|{expires_at:.6f}".encode("utf-8")
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def _purge_expired_locked(self, now: float) -> None:
        expired = [tid for tid, rec in self._tokens.items() if now > rec["expires_at"]]
        for tid in expired:
            self._tokens.pop(tid, None)

    def create_token(
        self,
        owner: str,
        action: str,
        payload: dict[str, Any],
        ttl: float | None = None,
    ) -> dict[str, Any]:
        """Issue a single-use confirmation token bound to owner, action, and payload."""
        owner_clean = (owner or "").strip()
        action_clean = (action or "").strip()
        if not owner_clean or ":" not in owner_clean:
            raise ValueError("Write confirmation token requires a valid owner ('tenant:uid')")
        if not action_clean:
            raise ValueError("Write confirmation token requires a non-empty action")
        if not isinstance(payload, dict):
            raise TypeError("Write confirmation payload must be a dict")

        now = time.time()
        effective_ttl = float(self.ttl if ttl is None else ttl)
        expires_at = now + effective_ttl
        nonce = secrets.token_urlsafe(24)
        payload_copy = copy.deepcopy(payload)
        payload_json = json.dumps(payload_copy, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        sig = self._sign(nonce, owner_clean, action_clean, payload_hash, expires_at)
        token_id = f"{nonce}.{sig[:24]}"

        record = {
            "token_id": token_id,
            "token": token_id,
            "owner": owner_clean,
            "action": action_clean,
            "payload": payload_copy,
            "payload_hash": payload_hash,
            "nonce": nonce,
            "sig": sig,
            "created_at": now,
            "expires_at": expires_at,
            "ttl": effective_ttl,
        }
        with self._lock:
            self._purge_expired_locked(now)
            self._tokens[token_id] = record

        return {
            "token_id": token_id,
            "token": token_id,
            "owner": owner_clean,
            "action": action_clean,
            "payload": copy.deepcopy(payload_copy),
            "created_at": now,
            "expires_at": expires_at,
            "ttl": effective_ttl,
        }

    def consume_token(self, owner: str, token_id: str) -> dict[str, Any] | None:
        """Consume a confirmation token once. Returns record dict if valid, else None."""
        owner_clean = (owner or "").strip()
        tid_clean = (token_id or "").strip()
        if not owner_clean or not tid_clean:
            return None

        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            record = self._tokens.get(tid_clean)
            if record is None:
                return None
            if record["owner"] != owner_clean:
                # Do not allow cross-owner probing; leave or reject without returning data
                return None
            # Atomically remove token so it can never be reused
            self._tokens.pop(tid_clean, None)

        if now > record["expires_at"]:
            return None

        expected_sig = self._sign(
            record["nonce"],
            record["owner"],
            record["action"],
            record["payload_hash"],
            record["expires_at"],
        )
        if not hmac.compare_digest(record["sig"], expected_sig):
            return None

        return {
            "token_id": record["token_id"],
            "token": record["token_id"],
            "owner": record["owner"],
            "action": record["action"],
            "payload": copy.deepcopy(record["payload"]),
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
        }


_BLOCKED_TEST_STATUSES = frozenset(
    {
        "live",
        "active",
        "open",
        "pending",
        "in_progress",
        "inprogress",
        "unsubmitted",
        "not_submitted",
        "not_started",
        "ongoing",
        "upcoming",
        "assigned",
        "due",
        "overdue",
        "draft",
        "started",
        "attempting",
        "unlocked",
        "available",
        "scheduled",
        "incomplete",
    }
)

_ALLOWED_TEST_STATUSES = frozenset(
    {
        "submitted",
        "graded",
        "evaluated",
        "completed",
        "published",
        "reviewed",
        "scored",
        "closed_submitted",
        "done",
        "finished",
        "marked",
        "returned",
        "released",
    }
)


def assert_not_live_test(
    item_status: str | None = None,
    submitted: bool | None = None,
    has_result: bool | None = None,
) -> None:
    """Raise PermissionError if an EOL test or assessment is unsubmitted/live/pending/in-progress."""
    if submitted is False:
        raise PermissionError(LIVE_TEST_ERROR_MSG)

    norm_status = ""
    if item_status is not None and isinstance(item_status, str):
        norm_status = re.sub(r"[\s\-]+", "_", item_status.strip().lower())

    if norm_status:
        if norm_status in _BLOCKED_TEST_STATUSES:
            raise PermissionError(LIVE_TEST_ERROR_MSG)
        for blocked_token in (
            "unsubmitted",
            "not_submitted",
            "in_progress",
            "inprogress",
            "pending",
            "live",
            "ongoing",
            "not_started",
        ):
            if blocked_token in norm_status:
                raise PermissionError(LIVE_TEST_ERROR_MSG)

    if has_result is False and submitted is not True and norm_status not in _ALLOWED_TEST_STATUSES:
        raise PermissionError(LIVE_TEST_ERROR_MSG)

    is_allowed_status = norm_status in _ALLOWED_TEST_STATUSES or any(
        tok in norm_status for tok in ("graded", "submitted", "evaluated", "completed", "scored")
    )

    if not is_allowed_status and submitted is not True and has_result is not True:
        raise PermissionError(LIVE_TEST_ERROR_MSG)
