# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for security primitives, encryption at rest, per-student cache isolation,
SSRF protection, single-use write confirmation tokens, and live test lockout.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cache as datacache
from app.config import load_or_create_secret_key
from app.db import Database
from app.security import (
    Cipher, WriteConfirmationManager, assert_not_live_test, owner_key,
    safe_fetch, sanitize_filename, validate_fetch_url,
)
from app.sessions import Session, SessionStore, import_local_indus_token


def _disk_bytes(db_path: Path) -> bytes:
    """Return SQLite main-file bytes plus WAL bytes when present."""
    wal = Path(f"{db_path}-wal")
    return db_path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")


def test_cipher_encrypt_decrypt_roundtrip():
    cipher = Cipher("test-passphrase-for-fernet-derivation")
    for raw, encrypt, decrypt in [
        (b"confidential-pdf-bytes-12345", cipher.encrypt_bytes, cipher.decrypt_bytes),
        ("super-secret-lms-access-token", cipher.encrypt_str, cipher.decrypt_str),
    ]:
        enc = encrypt(raw)
        assert enc != raw
        assert raw not in enc
        assert decrypt(enc) == raw

    # Class-level default Cipher methods also work
    cls_enc = Cipher.encrypt_str("class-level-secret")
    assert "class-level-secret" not in cls_enc
    assert Cipher.decrypt_str(cls_enc) == "class-level-secret"


def test_auto_generated_secret_key_persisted_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DOOMED_SECRET_KEY", raising=False)
    key1 = load_or_create_secret_key(tmp_path)
    key_file = tmp_path / ".secret_key"
    assert key_file.exists()
    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600
    # Second call loads the exact same persisted key
    assert load_or_create_secret_key(tmp_path) == key1


def test_sessions_sqlite_fernet_encryption_at_rest(tmp_path: Path):
    db_file = tmp_path / "sessions.sqlite"
    store = SessionStore(db_file)
    access = "PLAINTEXT_ACCESS_TOKEN_DO_NOT_LEAK_998877"
    refresh = "PLAINTEXT_REFRESH_TOKEN_DO_NOT_LEAK_112233"
    cache_val = '{"secret_grade":"A*","feedback":"Confidential teacher remark"}'
    sess = Session(
        access=access,
        refresh=refresh,
        tenant="school-tenant-1",
        user={"id": "student-uid-1", "full_name": "Alice Student", "email": "alice@indus.test"},
    )
    sid = store.add(sess)
    store.set_cache(f"payload:{sess.owner}", cache_val)
    store.close()

    # Check 0600 file permissions
    assert stat.S_IMODE(os.stat(db_file).st_mode) == 0o600

    # Inspect raw SQLite + WAL bytes on disk: plaintext tokens and cached JSON must NOT appear
    raw_disk_bytes = _disk_bytes(db_file)
    assert access.encode("utf-8") not in raw_disk_bytes
    assert refresh.encode("utf-8") not in raw_disk_bytes
    assert b"Confidential teacher remark" not in raw_disk_bytes

    # Reopen store and verify decryption works transparently
    store2 = SessionStore(db_file)
    rehydrated = store2.get(sid)
    assert rehydrated is not None
    assert (rehydrated.access, rehydrated.refresh) == (access, refresh)
    assert rehydrated.owner == "school-tenant-1:student-uid-1"
    assert store2.get_cache(f"payload:{rehydrated.owner}", max_age=300) == cache_val
    store2.close()


def test_import_local_indus_token(tmp_path: Path):
    token_file = tmp_path / "indus_token.json"
    token_file.write_text(json.dumps({
        "access": "access-tok-xyz",
        "refresh": "refresh-tok-xyz",
        "user": {
            "id": "uid-42",
            "email": "student42@indusschool.com",
            "full_name": "Student FortyTwo",
            "roles": [{"key": "STUDENT", "tenant_id": "tenant-indus-blr"}],
        },
    }), encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.sqlite")
    sess = import_local_indus_token(store, token_file)
    assert sess is not None and sess.sid is not None
    assert sess.owner == "tenant-indus-blr:uid-42"
    assert sess.access == "access-tok-xyz"
    store.close()


def test_per_student_cache_and_db_isolation_same_tenant(tmp_path: Path):
    """Student A and Student B on the SAME school tenant must never see each other's
    L1/L2 cache, file_access ACLs, encrypted file_pages, or lms_files_fts search results.
    """
    store = SessionStore(tmp_path / "sessions.sqlite")
    datacache.clear()
    datacache.set_store(store)

    tenant, uid_a, uid_b = "shared-school-tenant", "student-alpha", "student-bravo"
    owner_a, owner_b = owner_key(tenant, uid_a), owner_key(tenant, uid_b)

    # 1. Populate L1 + L2 payload and files for Student A
    payload_a, _ = datacache.get_or_build(
        "sid-a",
        lambda files: {"student": "Alpha", "grades": [95], "resources": files or []},
        tenant=tenant,
        uid=uid_a,
    )
    files_a = [{"ref": "lms://resource/r1/f1", "name": "Alpha_Private_IA.pdf"}]
    datacache.put_files("sid-a", files_a, tenant=tenant, uid=uid_a)
    assert payload_a["student"] == "Alpha"

    # Evict L1 to force L2 lookup
    datacache.clear()

    # Student B on the same tenant requests files and payload
    entry_b = datacache.get_entry("sid-b", tenant=tenant, uid=uid_b)
    assert entry_b is not None
    assert datacache.get_files(entry_b, allow_stale=True, tenant=tenant, uid=uid_b) is None
    assert datacache.get_payload(entry_b, allow_stale=True, tenant=tenant, uid=uid_b) is None

    # Student A's new session on same tenant + uid_a recovers Student A's L2 cache
    entry_a2 = datacache.get_entry("sid-a-new", tenant=tenant, uid=uid_a)
    assert entry_a2 is not None
    assert datacache.get_files(entry_a2, tenant=tenant, uid=uid_a) == files_a

    # 2. Database file_access, file_pages encryption, and FTS5 isolation
    db = Database(tmp_path / "doomed.sqlite")
    private_sha, ib_sha = "a" * 64, "b" * 64
    db.upsert_file(sha256=private_sha, source_type="lms", filename="Alpha_Physics_Feedback.pdf",
                   blob_path="blobs/private/aa/aaaa.enc", encrypted=1, subject="Physics")
    db.upsert_file(sha256=ib_sha, source_type="ib", filename="IB_Physics_Paper1.pdf",
                   blob_path="blobs/ib/bb/bbbb.pdf", encrypted=0, subject="Physics")

    # Grant access ONLY to Student A for private_sha
    db.grant_file_access(owner=owner_a, ref="lms://resource/res-1/file-1", sha256=private_sha,
                         filename="Alpha_Physics_Feedback.pdf", subject="Physics")
    db.put_file_page(private_sha, 1, "Private page 1 commentary for Alpha only", has_diagrams=True)

    # Verify raw doomed.sqlite does not contain plaintext page text
    assert b"Private page 1 commentary for Alpha only" not in _disk_bytes(tmp_path / "doomed.sqlite")

    # Student A can access private_sha; Student B cannot!
    assert db.can_access_file(owner_a, private_sha) is True
    assert db.can_access_file(owner_b, private_sha) is False
    page_a = db.get_file_page(owner_a, private_sha, 1)
    assert page_a is not None and page_a["text"] == "Private page 1 commentary for Alpha only"

    with pytest.raises(PermissionError):
        db.get_file_page(owner_b, private_sha, 1)

    # Both students can access public IB catalog resource
    assert db.can_access_file(owner_a, ib_sha) is True
    assert db.can_access_file(owner_b, ib_sha) is True

    # 3. FTS5 search isolation on same tenant
    db.upsert_lms_file_meta(owner=owner_a, ref="lms://resource/r-alpha/f-alpha", subject="Physics HL",
                            folder_path="Unit 3 / Wave Phenomena", title="Doppler Effect Teacher Notes",
                            filename="doppler_notes_alpha.pdf", teacher="Dr. Rao", ext="pdf")
    db.upsert_lms_file_meta(owner=owner_b, ref="lms://resource/r-bravo/f-bravo", subject="Economics SL",
                            folder_path="Macroeconomics", title="Aggregate Demand Notes",
                            filename="ad_as_bravo.pdf", teacher="Ms. Kapoor", ext="pdf")

    hits_a_doppler = db.search_lms_files(owner_a, "Doppler")
    assert len(hits_a_doppler) == 1
    assert hits_a_doppler[0]["ref"] == "lms://resource/r-alpha/f-alpha"

    # Student B searching for Student A's file gets zero results
    assert db.search_lms_files(owner_b, "Doppler") == []
    # Student A searching for Student B's file gets zero results
    assert db.search_lms_files(owner_a, "Aggregate") == []

    db.close()
    store.close()


@pytest.mark.parametrize(
    "blocked_url",
    [
        "",
        "http://example.com/file.pdf",
        "ftp://example.com/file.pdf",
        "https://127.0.0.1/secret",
        "https://127.0.0.53/secret",
        "https://169.254.169.254/latest/meta-data/",
        "https://192.168.1.1/admin",
        "https://10.0.0.1/internal",
        "https://172.16.0.1/internal",
        "https://172.31.255.255/internal",
        "https://localhost/api",
        "https://sub.localhost/api",
        "https://[::1]/secret",
        "https://[fe80::1]/secret",
        "https://[fc00::1]/secret",
        "https://example.com:8443/file.pdf",
        "https://example.com:80/file.pdf",
        "https://user:pass@example.com/file.pdf",
        "https://admin@example.com/file.pdf",
    ],
)
def test_ssrf_guard_rejects_unsafe_urls(blocked_url: str):
    with pytest.raises((ValueError, PermissionError)):
        validate_fetch_url(blocked_url, allow_local_ib=False)


def test_ssrf_guard_blocks_dns_rebinding_to_private_ip():
    fake_addrinfo = [(2, 1, 6, "", ("169.254.169.254", 443))]
    with mock.patch("socket.getaddrinfo", return_value=fake_addrinfo), \
         pytest.raises((ValueError, PermissionError)):
        validate_fetch_url("https://evil.example.com/resource.pdf")


def test_ssrf_guard_allows_public_https_and_validates_redirect_hops():
    public_addrinfo = [(2, 1, 6, "", ("93.184.216.34", 443))]
    redirect_resp = mock.MagicMock(
        status_code=302, headers={"location": "https://169.254.169.254/latest/meta-data/"}
    )
    fake_session = mock.MagicMock()
    fake_session.get.return_value = redirect_resp

    with mock.patch("socket.getaddrinfo", return_value=public_addrinfo):
        validate_fetch_url("https://api.induslms.com/files/doc.pdf")
        # Redirect hop to cloud metadata IP must be blocked by safe_fetch
        with pytest.raises((ValueError, PermissionError)):
            safe_fetch("https://api.induslms.com/files/doc.pdf", session=fake_session)


def test_write_confirmation_manager_single_use_and_expiry():
    mgr = WriteConfirmationManager(ttl=600)
    owner_a, owner_b = "tenant1:studentA", "tenant1:studentB"
    payload = {"assignment_id": "fa-101", "comment": "Here is my submission"}
    tok = mgr.create_token(owner_a, "submit_fa_assessment", payload)
    assert (tok["action"], tok["owner"]) == ("submit_fa_assessment", owner_a)

    # Wrong owner cannot consume Student A's token
    assert mgr.consume_token(owner_b, tok["token_id"]) is None

    # First consumption by rightful owner succeeds
    consumed = mgr.consume_token(owner_a, tok["token_id"])
    assert consumed is not None
    assert consumed["action"] == "submit_fa_assessment"
    assert consumed["payload"] == payload

    # Second consumption attempt fails (single-use anti-replay)
    assert mgr.consume_token(owner_a, tok["token_id"]) is None

    # Expired token fails
    expired_tok = mgr.create_token(owner_a, "mark_read", {"id": "n1"}, ttl=-1.0)
    assert mgr.consume_token(owner_a, expired_tok["token_id"]) is None


@pytest.mark.parametrize(
    "status,submitted,has_result",
    [
        ("live", None, None),
        ("in_progress", False, False),
        ("in-progress", None, None),
        ("pending", False, False),
        ("unsubmitted", False, False),
        ("not_submitted", None, None),
        ("open", False, None),
        ("upcoming", None, None),
        ("assigned", False, False),
        (None, False, False),
        (None, None, None),
        ("graded", False, True),  # explicit submitted=False always blocks
    ],
)
def test_assert_not_live_test_blocks_unsubmitted_or_live(
    status: str | None, submitted: bool | None, has_result: bool | None
):
    with pytest.raises(
        PermissionError,
        match="Live test protection: assistant only explains graded/submitted work.",
    ):
        assert_not_live_test(status, submitted=submitted, has_result=has_result)


@pytest.mark.parametrize(
    "status,submitted,has_result",
    [
        ("graded", True, True),
        ("submitted", True, False),
        ("evaluated", True, True),
        ("completed", True, True),
        ("published", None, True),
        (None, True, True),
        (None, True, None),
    ],
)
def test_assert_not_live_test_allows_graded_or_submitted(
    status: str | None, submitted: bool | None, has_result: bool | None
):
    assert_not_live_test(status, submitted=submitted, has_result=has_result)


def test_sanitize_filename():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("..\\..\\windows\\system32\\cmd.exe") == "cmd.exe"
    assert sanitize_filename('bad\x00\x1f"name";.pdf') == "badname.pdf"
    assert sanitize_filename("...") == "file"
    assert sanitize_filename("") == "file"
