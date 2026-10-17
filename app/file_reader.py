# SPDX-License-Identifier: GPL-3.0-or-later
"""Secure multimodal file reader and document ingestion pipeline.

Enforces:
- Private blobs (LMS, user uploads) are Fernet-encrypted at rest on disk.
- Public IB blobs are verified by SHA-256 and stored plaintext.
- Strict per-owner ACL check (Database.can_access_file) before reading any bytes or page text.
- Multimodal page rasterization (PyMuPDF pixmaps to PNG data URLs) for vision LLM inspection.
- Rich text extraction with Markdown table formatting for DOCX, PPTX, XLSX, and PDF.
"""

from __future__ import annotations

import base64
import hashlib
import io
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

import fitz  # type: ignore  # PyMuPDF
from docx import Document  # type: ignore
from openpyxl import load_workbook  # type: ignore
from pptx import Presentation  # type: ignore

from app import config
from app.db import Database
from app.security import Cipher, safe_fetch, sanitize_filename


def _resolve_blob_dirs() -> tuple[Path, Path]:
    data_dir = config.ensure_data_dir()
    priv_dir = data_dir / "blobs" / "private"
    ib_dir = data_dir / "blobs" / "ib"
    priv_dir.mkdir(parents=True, exist_ok=True)
    ib_dir.mkdir(parents=True, exist_ok=True)
    return priv_dir, ib_dir


def parse_page_range(spec: str | list[int] | None, max_pages: int) -> list[int]:
    """Parse page specification like '1-5', '1,3,7-9', [2, 4], or None (default 1..min(5, max))."""
    if max_pages <= 0:
        return []
    if spec is None or spec == "":
        return list(range(1, min(6, max_pages + 1)))

    if isinstance(spec, list):
        pages = [int(p) for p in spec if isinstance(p, (int, str)) and str(p).isdigit()]
        return sorted(list({p for p in pages if 1 <= p <= max_pages}))

    cleaned = str(spec).strip()
    out: set[int] = set()
    parts = cleaned.split(",")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            sub = part.split("-", 1)
            try:
                start = int(sub[0].strip())
                end = int(sub[1].strip())
                for i in range(max(1, start), min(max_pages, end) + 1):
                    out.add(i)
            except ValueError:
                continue
        elif part.isdigit():
            val = int(part)
            if 1 <= val <= max_pages:
                out.add(val)

    res = sorted(list(out))
    return res if res else list(range(1, min(6, max_pages + 1)))


def extract_document_pages(
    raw_bytes: bytes, filename: str, mime: str = ""
) -> list[dict[str, Any]]:
    """Extract structured text and diagram flags per page (1-based index).
    Never crashes on corrupt files — returns a single error page."""
    ext = Path(filename).suffix.lower().lstrip(".")
    mime_type = mime.lower() if mime else (mimetypes.guess_type(filename)[0] or "").lower()

    # 1. PDF
    if ext == "pdf" or "pdf" in mime_type:
        try:
            doc = fitz.open(stream=raw_bytes, filetype="pdf")
            pages: list[dict[str, Any]] = []
            toc_lines = []
            try:
                toc = doc.get_toc()
                if toc:
                    toc_lines.append("## Table of Contents")
                    for level, title, page_no in toc:
                        indent = "  " * (max(1, level) - 1)
                        toc_lines.append(f"{indent}- {title} (p. {page_no})")
            except Exception:
                pass

            toc_header = "\n".join(toc_lines) + "\n\n" if toc_lines else ""

            for idx in range(len(doc)):
                page_num = idx + 1
                page = doc[idx]
                text = page.get_text() or ""
                if page_num == 1 and toc_header:
                    text = toc_header + text

                has_diagrams = False
                try:
                    imgs = page.get_images()
                    drawings = page.get_drawings()
                    if len(imgs) > 0 or len(drawings) > 0:
                        has_diagrams = True
                except Exception:
                    pass

                pages.append({
                    "page_num": page_num,
                    "text": text.strip() or f"[Page {page_num}: No readable text (vector/scanned content)]",
                    "has_diagrams": has_diagrams,
                })
            return pages if pages else [{"page_num": 1, "text": "[Empty PDF]", "has_diagrams": False}]
        except Exception as e:
            return [{"page_num": 1, "text": f"[Corrupt or unreadable PDF: {e}]", "has_diagrams": False}]

    # 2. DOCX
    elif ext == "docx" or "wordprocessingml" in mime_type:
        try:
            doc = Document(io.BytesIO(raw_bytes))
            blocks: list[str] = []
            has_images = False

            for p in doc.paragraphs:
                txt = p.text.strip()
                if not txt:
                    continue
                if p.style.name.startswith("Heading"):
                    blocks.append(f"### {txt}")
                else:
                    blocks.append(txt)

            for table in doc.tables:
                table_lines = []
                for row_idx, row in enumerate(table.rows):
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    line = "| " + " | ".join(cells) + " |"
                    table_lines.append(line)
                    if row_idx == 0:
                        sep = "| " + " | ".join(["---"] * len(cells)) + " |"
                        table_lines.append(sep)
                if table_lines:
                    blocks.append("\n" + "\n".join(table_lines) + "\n")

            full_text = "\n\n".join(blocks).strip()
            # Split into ~3000-char pages
            chunk_size = 3000
            pages = []
            if not full_text:
                pages.append({"page_num": 1, "text": "[Empty DOCX document]", "has_diagrams": False})
            else:
                for i in range(0, len(full_text), chunk_size):
                    page_num = (i // chunk_size) + 1
                    pages.append({
                        "page_num": page_num,
                        "text": full_text[i : i + chunk_size].strip(),
                        "has_diagrams": has_images,
                    })
            return pages
        except Exception as e:
            return [{"page_num": 1, "text": f"[Corrupt DOCX: {e}]", "has_diagrams": False}]

    # 3. PPTX
    elif ext == "pptx" or "presentationml" in mime_type:
        try:
            prs = Presentation(io.BytesIO(raw_bytes))
            pages = []
            for idx, slide in enumerate(prs.slides):
                page_num = idx + 1
                lines = []
                has_diagram = False
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        txt = shape.text_frame.text.strip()
                        if txt:
                            lines.append(txt)
                    elif shape.has_table:
                        tbl = shape.table
                        tlines = []
                        for r_idx, row in enumerate(tbl.rows):
                            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                            tlines.append("| " + " | ".join(cells) + " |")
                            if r_idx == 0:
                                tlines.append("| " + " | ".join(["---"] * len(cells)) + " |")
                        if tlines:
                            lines.append("\n" + "\n".join(tlines) + "\n")
                    else:
                        has_diagram = True

                try:
                    if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                        notes = slide.notes_slide.notes_text_frame.text.strip()
                        if notes:
                            lines.append(f"\n*Speaker Notes*: {notes}")
                except Exception:
                    pass

                text_content = "\n\n".join(lines).strip()
                pages.append({
                    "page_num": page_num,
                    "text": text_content or f"[Slide {page_num}: Visual content only]",
                    "has_diagrams": has_diagram,
                })
            return pages if pages else [{"page_num": 1, "text": "[Empty Presentation]", "has_diagrams": False}]
        except Exception as e:
            return [{"page_num": 1, "text": f"[Corrupt PPTX: {e}]", "has_diagrams": False}]

    # 4. XLSX
    elif ext in ("xlsx", "xls") or "spreadsheetml" in mime_type:
        try:
            wb = load_workbook(io.BytesIO(raw_bytes), data_only=True)
            pages = []
            for idx, sheet_name in enumerate(wb.sheetnames):
                page_num = idx + 1
                sheet = wb[sheet_name]
                tlines = [f"### Sheet: {sheet_name}\n"]
                row_count = 0
                for r_idx, row in enumerate(sheet.iter_rows(values_only=True)):
                    if not any(row):
                        continue
                    cells = [str(c or "").strip().replace("\n", " ") for c in row]
                    tlines.append("| " + " | ".join(cells) + " |")
                    if row_count == 0:
                        tlines.append("| " + " | ".join(["---"] * len(cells)) + " |")
                    row_count += 1
                    if row_count > 100:  # Truncate long sheets
                        tlines.append("| ... [sheet truncated after 100 rows] |")
                        break
                pages.append({
                    "page_num": page_num,
                    "text": "\n".join(tlines),
                    "has_diagrams": False,
                })
            return pages if pages else [{"page_num": 1, "text": "[Empty Workbook]", "has_diagrams": False}]
        except Exception as e:
            return [{"page_num": 1, "text": f"[Corrupt XLSX: {e}]", "has_diagrams": False}]

    # 5. Image
    elif ext in ("png", "jpg", "jpeg", "webp", "gif", "svg") or "image" in mime_type:
        return [{
            "page_num": 1,
            "text": f"Image file: {filename} ({len(raw_bytes)} bytes)",
            "has_diagrams": True,
        }]

    # 6. Plain text / Markdown / Code
    else:
        try:
            text = raw_bytes.decode("utf-8", errors="replace")
            lines = text.splitlines()
            pages = []
            page_size = 100
            for i in range(0, max(1, len(lines)), page_size):
                page_num = (i // page_size) + 1
                chunk = "\n".join(lines[i : i + page_size])
                pages.append({
                    "page_num": page_num,
                    "text": chunk or "[Empty text]",
                    "has_diagrams": False,
                })
            return pages
        except Exception as e:
            return [{"page_num": 1, "text": f"[Unreadable text file: {e}]", "has_diagrams": False}]


def ingest_bytes(
    db: Database,
    owner: str,
    raw_bytes: bytes,
    filename: str,
    source_type: str,
    ref: str,
    mime: str = "",
    upstream_url: str = "",
    subject: str = "",
    folder: str = "",
    teacher: str = "",
    level: str = "",
    rtype: str = "",
) -> dict[str, Any]:
    """Ingest raw file bytes:
    1. Computes SHA-256.
    2. Writes blob to disk (Fernet-encrypted if private LMS/upload, plaintext if public IB).
    3. Extracts document pages & stores encrypted text in db.file_pages.
    4. Upserts db.files & grants access in db.file_access for owner.
    """
    if len(raw_bytes) > config.MAX_FILE_BYTES:
        raise ValueError(
            f"File exceeds maximum allowed size ({len(raw_bytes)} > {config.MAX_FILE_BYTES} bytes)."
        )

    sha256 = hashlib.sha256(raw_bytes).hexdigest().lower()
    is_private = source_type in ("lms", "upload")
    priv_dir, ib_dir = _resolve_blob_dirs()

    if is_private:
        target_dir = priv_dir / sha256[:2]
        target_dir.mkdir(parents=True, exist_ok=True)
        blob_path = target_dir / f"{sha256}.enc"
        if not blob_path.exists():
            encrypted_bytes = Cipher.encrypt_bytes(raw_bytes)
            blob_path.write_bytes(encrypted_bytes)
    else:
        target_dir = ib_dir / sha256[:2]
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_filename(filename or f"{sha256}.bin")
        blob_path = target_dir / f"{sha256}-{safe_name}"
        if not blob_path.exists():
            blob_path.write_bytes(raw_bytes)

    pages = extract_document_pages(raw_bytes, filename, mime=mime)
    db.put_file_pages(sha256, pages)

    db.upsert_file(
        sha256=sha256,
        source_type=source_type,
        upstream_url=upstream_url,
        filename=filename,
        mime=mime or mimetypes.guess_type(filename)[0] or "application/octet-stream",
        size_bytes=len(raw_bytes),
        page_count=len(pages),
        blob_path=str(blob_path),
        encrypted=1 if is_private else 0,
        subject=subject,
        level=level,
        rtype=rtype,
    )

    db.grant_file_access(
        owner=owner,
        ref=ref,
        sha256=sha256,
        filename=filename,
        subject=subject,
        folder=folder,
        teacher=teacher,
    )

    return {
        "sha256": sha256,
        "ref": ref,
        "filename": filename,
        "mime": mime or mimetypes.guess_type(filename)[0] or "",
        "size_bytes": len(raw_bytes),
        "page_count": len(pages),
        "source_type": source_type,
        "has_diagrams": any(p.get("has_diagrams") for p in pages),
    }


def get_file_bytes(db: Database, owner: str, ref_or_sha: str) -> tuple[dict[str, Any], bytes]:
    """Verify owner has access, read blob, decrypt if private, return (file_row, raw_bytes).
    Raises PermissionError if owner has no access."""
    clean_target = ref_or_sha.strip()
    file_row = None
    sha256 = ""

    if "://" in clean_target:
        acc = db.get_file_access(owner, clean_target)
        if acc:
            sha256 = acc["sha256"]
        elif clean_target.startswith("ib://"):
            sha256 = clean_target[5:].strip().lower()
        else:
            # Check if ref exists in the system under any owner
            ref_row = db.query_one("SELECT sha256 FROM file_access WHERE ref = ?", (clean_target,))
            if ref_row:
                sha256 = ref_row["sha256"]
            else:
                raise FileNotFoundError(f"File ref not found: {ref_or_sha}")
    else:
        sha256 = clean_target.lower()

    if not sha256:
        raise FileNotFoundError(f"Could not resolve file identifier: {ref_or_sha}")

    if not db.can_access_file(owner, sha256):
        raise PermissionError(f"Access denied to file '{ref_or_sha}' for owner '{owner}'.")

    file_row = db.get_file(sha256)
    if not file_row:
        raise FileNotFoundError(f"File record not found in database: {sha256}")

    blob_path = Path(file_row["blob_path"])
    if not blob_path.exists():
        raise FileNotFoundError(f"Blob file missing on disk: {blob_path}")

    raw_disk = blob_path.read_bytes()
    if file_row["encrypted"]:
        content_bytes = Cipher.decrypt_bytes(raw_disk)
    else:
        content_bytes = raw_disk

    db.touch_file(sha256)
    return file_row, content_bytes


def fetch_and_cache_ref(
    db: Database, owner: str, ref: str, session: Any = None
) -> dict[str, Any]:
    """Ensure a file ref (ib://, lms://resource/..., lms://url/...) is cached in the blob store.
    If already cached and disk blob exists, returns immediately without redownloading."""
    ref = ref.strip()
    if ref.startswith("ib://"):
        target_sha = ref[5:].strip().lower()
        existing = db.get_file(target_sha)
        if existing and Path(existing["blob_path"]).exists():
            db.grant_file_access(owner, ref, target_sha, filename=existing["filename"])
            return existing

        base_url = config.get_setting("IB_SCRAPE_BASE_URL", "https://ib-scrape.sidevv.xyz")
        url = f"{base_url}/download/{target_sha}"
        resp = safe_fetch(url, stream=True)
        if resp.status_code >= 400:
            raise LMSUpstreamError(f"Failed to download IB resource {target_sha}: HTTP {resp.status_code}")

        cdisp = resp.headers.get("content-disposition", "")
        m = re.search(r'filename="?([^";]+)"?', cdisp)
        fname = m.group(1) if m else f"{target_sha}.pdf"
        raw = resp.content
        return ingest_bytes(
            db=db,
            owner=owner,
            raw_bytes=raw,
            filename=fname,
            source_type="ib",
            ref=ref,
            mime=resp.headers.get("content-type", ""),
            upstream_url=url,
        )

    elif ref.startswith("lms://resource/"):
        parts = ref[15:].split("/")
        if len(parts) < 2:
            raise ValueError(f"Invalid lms resource ref: {ref}")
        resource_id, file_id = parts[0], parts[1]

        existing_acc = db.get_file_access(owner, ref)
        if existing_acc:
            f_row = db.get_file(existing_acc["sha256"])
            if f_row and Path(f_row["blob_path"]).exists():
                return f_row

        if not session or not session.access:
            raise LMSNotAuthenticatedError("Active LMS session required to download resource.")

        # Download from Indus LMS API
        api_base = "https://api.induslms.com"
        dl_url = f"{api_base}/api/v1/tenants/{session.tenant}/resources/{resource_id}/files/{file_id}/content/?disposition=attachment"
        headers = {"Authorization": f"Bearer {session.access}"}
        resp = safe_fetch(dl_url, headers=headers, stream=True)
        if resp.status_code >= 400:
            raise LMSUpstreamError(f"Indus LMS file download failed: HTTP {resp.status_code}")

        cdisp = resp.headers.get("content-disposition", "")
        m = re.search(r'filename="?([^";]+)"?', cdisp)
        fname = m.group(1) if m else f"{file_id}.pdf"
        raw = resp.content

        return ingest_bytes(
            db=db,
            owner=owner,
            raw_bytes=raw,
            filename=fname,
            source_type="lms",
            ref=ref,
            mime=resp.headers.get("content-type", ""),
            upstream_url=dl_url,
        )

    elif ref.startswith("lms://url/"):
        b64_url = ref[10:]
        padding = "=" * (-len(b64_url) % 4)
        target_url = base64.urlsafe_b64decode(b64_url + padding).decode("utf-8")

        existing_acc = db.get_file_access(owner, ref)
        if existing_acc:
            f_row = db.get_file(existing_acc["sha256"])
            if f_row and Path(f_row["blob_path"]).exists():
                return f_row

        resp = safe_fetch(target_url, stream=True)
        if resp.status_code >= 400:
            raise LMSUpstreamError(f"Attachment download failed: HTTP {resp.status_code}")

        cdisp = resp.headers.get("content-disposition", "")
        m = re.search(r'filename="?([^";]+)"?', cdisp)
        fname = m.group(1) if m else Path(target_url).name or "attachment"
        raw = resp.content

        return ingest_bytes(
            db=db,
            owner=owner,
            raw_bytes=raw,
            filename=fname,
            source_type="lms",
            ref=ref,
            mime=resp.headers.get("content-type", ""),
            upstream_url=target_url,
        )

    else:
        raise ValueError(f"Unsupported file ref scheme: {ref}")


def read_document(
    db: Database,
    owner: str,
    ref_or_sha: str,
    pages: str | list[int] | None = "1-5",
    session: Any = None,
) -> dict[str, Any]:
    """Agent tool: read decrypted text and diagram status for specified pages.
    Guarantees strict per-owner ACL check."""
    if "://" in ref_or_sha:
        fetch_and_cache_ref(db, owner, ref_or_sha, session=session)

    file_row, _ = get_file_bytes(db, owner, ref_or_sha)
    sha256 = file_row["sha256"]
    total_pages = file_row.get("page_count", 1)
    wanted_pages = parse_page_range(pages, total_pages)

    # Fetch all pages for sha256 securely with owner ACL check
    all_pages = db.get_file_pages(owner, sha256, start_page=1, end_page=total_pages)
    wanted_set = set(wanted_pages)
    db_pages = [p for p in all_pages if p["page_num"] in wanted_set]
    out_pages = []
    has_diagrams_found = False

    for p in db_pages:
        d_flag = bool(p.get("has_diagrams"))
        if d_flag:
            has_diagrams_found = True
        out_pages.append({
            "page_num": p["page_num"],
            "text": p["text"],
            "has_diagrams": d_flag,
        })

    hint = ""
    if has_diagrams_found:
        hint = "Note: Pages with has_diagrams=True contain diagrams or geometry figures. Use inspect_document_pages to view them visually."

    return {
        "sha256": sha256,
        "ref": ref_or_sha,
        "filename": file_row.get("filename"),
        "page_count": total_pages,
        "requested_pages": wanted_pages,
        "pages": out_pages,
        "hint": hint,
    }


def search_within_document(
    db: Database,
    owner: str,
    ref_or_sha: str,
    query: str,
    top_k: int = 5,
    session: Any = None,
) -> dict[str, Any]:
    """Agent tool: search query across all pages of a file with term-frequency ranking and snippets."""
    if "://" in ref_or_sha:
        fetch_and_cache_ref(db, owner, ref_or_sha, session=session)

    file_row, _ = get_file_bytes(db, owner, ref_or_sha)
    sha256 = file_row["sha256"]
    total_pages = file_row.get("page_count", 1)

    all_pages = db.get_file_pages(owner, sha256, start_page=1, end_page=total_pages)
    q_tokens = [tok.lower() for tok in re.findall(r"\w+", query) if tok]
    if not q_tokens:
        return {"sha256": sha256, "query": query, "matches": []}

    scored: list[tuple[int, dict[str, Any], str]] = []
    for page in all_pages:
        text = page.get("text", "")
        text_lower = text.lower()
        score = sum(text_lower.count(tok) for tok in q_tokens)
        if score > 0:
            # Extract snippet around first match
            first_idx = min(text_lower.find(tok) for tok in q_tokens if tok in text_lower)
            start = max(0, first_idx - 100)
            end = min(len(text), first_idx + 250)
            snip = ("..." if start > 0 else "") + text[start:end].strip() + ("..." if end < len(text) else "")
            scored.append((score, page, snip))

    scored.sort(key=lambda x: x[0], reverse=True)
    matches = [
        {
            "page_num": item[1]["page_num"],
            "score": item[0],
            "snippet": item[2],
            "has_diagrams": bool(item[1].get("has_diagrams")),
        }
        for item in scored[:top_k]
    ]

    return {
        "sha256": sha256,
        "filename": file_row.get("filename"),
        "query": query,
        "matches": matches,
    }


def inspect_document_pages(
    db: Database,
    owner: str,
    ref_or_sha: str,
    pages: list[int] | str,
    dpi: int = 150,
    session: Any = None,
) -> list[dict[str, Any]]:
    """Agent tool: rasterize document pages to PNG and return base64 data URLs for vision inspection.
    Capped at 6 pages per call to protect context."""
    if "://" in ref_or_sha:
        fetch_and_cache_ref(db, owner, ref_or_sha, session=session)

    file_row, raw_bytes = get_file_bytes(db, owner, ref_or_sha)
    total_pages = file_row.get("page_count", 1)
    wanted = parse_page_range(pages, total_pages)[:6]  # Cap at 6 pages

    filename = file_row.get("filename", "")
    ext = Path(filename).suffix.lower().lstrip(".")
    mime = file_row.get("mime", "")

    effective_dpi = min(max(dpi, 72), 220)
    results = []

    # A. PDF rasterization via PyMuPDF
    if ext == "pdf" or "pdf" in mime:
        doc = fitz.open(stream=raw_bytes, filetype="pdf")
        for p in wanted:
            if 1 <= p <= len(doc):
                page = doc[p - 1]
                pix = page.get_pixmap(dpi=effective_dpi)
                png_bytes = pix.tobytes("png")
                b64_str = base64.b64encode(png_bytes).decode("ascii")
                results.append({
                    "page_num": p,
                    "mime": "image/png",
                    "data_url": f"data:image/png;base64,{b64_str}",
                    "width": pix.width,
                    "height": pix.height,
                })
        return results

    # B. Image files
    elif ext in ("png", "jpg", "jpeg", "webp", "gif") or "image" in mime:
        img_mime = f"image/{ext if ext != 'jpg' else 'jpeg'}"
        b64_str = base64.b64encode(raw_bytes).decode("ascii")
        return [{
            "page_num": 1,
            "mime": img_mime,
            "data_url": f"data:{img_mime};base64,{b64_str}",
            "width": 800,
            "height": 600,
        }]

    else:
        # For non-renderable formats, return note
        return [{
            "page_num": 1,
            "error": f"Visual rasterization not supported for format: {ext}",
        }]


def sweep_blob_cache(
    db: Database,
    max_private_mb: int | None = None,
    max_ib_mb: int | None = None,
) -> int:
    """Evict LRU blobs if cache exceeds configured megabytes."""
    priv_limit = (max_private_mb or config.MAX_PRIVATE_CACHE_MB) * 1024 * 1024
    ib_limit = (max_ib_mb or config.MAX_IB_CACHE_MB) * 1024 * 1024
    evicted = 0

    priv_dir, ib_dir = _resolve_blob_dirs()

    # Query all files sorted by last_accessed ASC
    rows = db.query_all("SELECT sha256, source_type, size_bytes, blob_path FROM files ORDER BY last_accessed ASC")
    priv_bytes = sum(r["size_bytes"] for r in rows if r["source_type"] in ("lms", "upload"))
    ib_bytes = sum(r["size_bytes"] for r in rows if r["source_type"] == "ib")

    for r in rows:
        sha = r["sha256"]
        stype = r["source_type"]
        size = r["size_bytes"]
        p = Path(r["blob_path"])

        if stype in ("lms", "upload") and priv_bytes > priv_limit:
            if p.exists():
                p.unlink()
            db.execute("DELETE FROM files WHERE sha256 = ?", (sha,))
            db.execute("DELETE FROM file_pages WHERE sha256 = ?", (sha,))
            priv_bytes -= size
            evicted += 1

        elif stype == "ib" and ib_bytes > ib_limit:
            if p.exists():
                p.unlink()
            db.execute("DELETE FROM files WHERE sha256 = ?", (sha,))
            db.execute("DELETE FROM file_pages WHERE sha256 = ?", (sha,))
            ib_bytes -= size
            evicted += 1

    return evicted
