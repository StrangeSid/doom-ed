# SPDX-License-Identifier: GPL-3.0-or-later
"""Client for the IB Resource Catalog API (https://ib-scrape.sidevv.xyz).

Provides full-text search, facet queries, metadata lookups, paper-to-markscheme
signature parsing and pairing, LMS course-to-IB mapping, and in-memory TTL caching.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
from collections import defaultdict
from typing import Any

import requests

from app.config import get_setting

logger = logging.getLogger(__name__)

DEFAULT_IB_BASE_URL = "https://ib-scrape.sidevv.xyz"
DEFAULT_CACHE_TTL = 600.0  # 10 minutes
_HEX_64_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# In-memory thread-safe TTL cache: cache_key -> (expires_at, deepcopied_data)
_CACHE: dict[tuple[Any, ...], tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()
_MAX_CACHE_ENTRIES = 1000

# Canonical subject pattern mapping (order by specificity: specific subjects before general)
SUBJECT_PATTERNS: list[tuple[str, str, str]] = [
    (
        r"math.*analysis.*approaches|math.*aa|mataa",
        "mathematics_aa",
        "mathematics analysis and approaches",
    ),
    (
        r"math.*applications.*interpretation|math.*ai|matai",
        "mathematics_ai",
        "mathematics applications and interpretation",
    ),
    (
        r"further.*math",
        "further_mathematics",
        "further mathematics",
    ),
    (
        r"mathematics|maths|math",
        "mathematics",
        "mathematics",
    ),
    (
        r"physics|physi",
        "physics",
        "physics",
    ),
    (
        r"chemistry|chemi",
        "chemistry",
        "chemistry",
    ),
    (
        r"biology|biolo",
        "biology",
        "biology",
    ),
    (
        r"computer[_\s]*science|compsci|\bcomps\b|\bcs\b",
        "computer_science",
        "computer science",
    ),
    (
        r"environmental[_\s]*systems|envsys|\bess\b",
        "environmental_systems_and_societies",
        "environmental systems societies",
    ),
    (
        r"design[_\s]*technology|\bdt\b",
        "design_technology",
        "design technology",
    ),
    (
        r"sports[_\s,]*(?:exercise|health)|\bsehs\b",
        "sports_exercise_health_science",
        "sports exercise health science",
    ),
    (
        r"business[_\s]*(?:and|&)[_\s]*management|business[_\s]*management|\bbusman\b|\bbusiness\b",
        "business_management",
        "business management",
    ),
    (
        r"economics|\becono\b",
        "economics",
        "economics",
    ),
    (
        r"history|\bhisto\b",
        "history",
        "history",
    ),
    (
        r"psychology|\bpsych\b",
        "psychology",
        "psychology",
    ),
    (
        r"geography|\bgeogr\b",
        "geography",
        "geography",
    ),
    (
        r"global[_\s]*politics|\bglopo\b",
        "global_politics",
        "global politics",
    ),
    (
        r"philosophy",
        "philosophy",
        "philosophy",
    ),
    (
        r"social.*anthropology",
        "social_cultural_anthropology",
        "social cultural anthropology",
    ),
    (
        r"theory[_\s]*of[_\s]*knowledge|\btok\b",
        "theory_of_knowledge",
        "theory of knowledge",
    ),
    (
        r"extended[_\s]*essay|\bee\b",
        "extended_essay",
        "extended essay",
    ),
    (
        r"english[_\s]*a[_\s]*lit|\benglish[_\s]*lit\b",
        "english_a_literature",
        "english a literature",
    ),
    (
        r"english[_\s]*a[_\s]*lang|\benglish[_\s]*lang",
        "english_a_language_literature",
        "english a language literature",
    ),
    (
        r"english[_\s]*b",
        "english_b",
        "english b",
    ),
    (
        r"french[_\s]*b",
        "french_b",
        "french b",
    ),
    (
        r"spanish[_\s]*b",
        "spanish_b",
        "spanish b",
    ),
]


def get_base_url() -> str:
    """Return the configured base URL for the IB scrape service, stripped of trailing slashes."""
    raw = get_setting("IB_SCRAPE_BASE_URL", DEFAULT_IB_BASE_URL)
    return (raw or DEFAULT_IB_BASE_URL).rstrip("/")


def clear_cache() -> None:
    """Clear all entries in the in-memory TTL cache."""
    with _CACHE_LOCK:
        _CACHE.clear()


def get_cached(key: tuple[Any, ...]) -> Any | None:
    """Look up key in in-memory TTL cache. Return deepcopy if valid, else None."""
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        expires_at, data = entry
        if time.time() >= expires_at:
            _CACHE.pop(key, None)
            return None
        return copy.deepcopy(data)


def set_cached(key: tuple[Any, ...], value: Any, ttl: float = DEFAULT_CACHE_TTL) -> None:
    """Store value in in-memory TTL cache with given TTL in seconds."""
    now = time.time()
    with _CACHE_LOCK:
        if len(_CACHE) >= _MAX_CACHE_ENTRIES:
            # Purge expired entries
            expired = [k for k, (exp, _) in _CACHE.items() if now >= exp]
            for k in expired:
                _CACHE.pop(k, None)
            # If still full, pop first entry
            if len(_CACHE) >= _MAX_CACHE_ENTRIES:
                first_k = next(iter(_CACHE))
                _CACHE.pop(first_k, None)
        _CACHE[key] = (now + ttl, copy.deepcopy(value))


def parse_paper_signature(name: str) -> dict[str, Any]:
    """Extract normalized IB signature from filenames, titles, or resource paths.

    Returns dict with keys:
        - subject_slug: str | None (e.g., 'physics', 'mathematics_aa', 'chemistry')
        - level: str | None ('HL', 'SL', 'MYP')
        - year: str | None ('2015'-'2026')
        - session: str | None ('may' or 'november')
        - paper_num: str | None ('1', '2', '3', '1a', '1b')
        - tz: str | None ('tz1', 'tz2', 'tz0', None)
        - is_markscheme: bool (True for markschemes/MS, False for question papers/exams)
    """
    if not name or not isinstance(name, str):
        return {
            "subject_slug": None,
            "level": None,
            "year": None,
            "session": None,
            "paper_num": None,
            "tz": None,
            "is_markscheme": False,
        }

    clean = name.replace("\\", "/").strip()
    low = clean.lower()

    # Detect markscheme vs paper
    is_ms = bool(
        re.search(
            r"mark[\s_-]?scheme|markschem|marking|(?:^|[_\-.\s/])ms(?:$|[_\-.\s/])|\bmks\b|_m(?:\.[a-z0-9]+)?$",
            clean,
            re.I,
        )
    )

    # Timezone: tz0, tz1, tz2, tz3
    m_tz = re.search(r"(?:^|[_\-.\s/])tz\s*([0-3])(?:$|[_\-.\s/])", clean, re.I)
    tz = f"tz{m_tz.group(1).lower()}" if m_tz else None

    # Paper number: 1, 2, 3, 1a, 1b, etc.
    m_p = re.search(r"(?<![a-zA-Z])paper[-_\s]*([1-3](?:[ab])?)(?![a-zA-Z])", clean, re.I)
    if not m_p:
        m_p = re.search(r"(?<![a-zA-Z])[hs]p([1-3](?:[ab])?)(?![a-zA-Z])", clean, re.I)
    if not m_p:
        m_p = re.search(r"(?:^|[_\-.\s/])p([1-3](?:[ab])?)(?:$|[_\-.\s/])", clean, re.I)
    paper_num = m_p.group(1).lower() if m_p else None

    # Level: HL, SL, or MYP
    m_lvl = re.search(r"(?<![A-Za-z])(HL|SL|MYP)(?![A-Za-z])", clean, re.I)
    if m_lvl:
        level = m_lvl.group(1).upper()
    else:
        m_code_lvl = re.search(r"(?<![A-Za-z])([HS])P[1-3](?![A-Za-z])", clean, re.I)
        level = ("HL" if m_code_lvl.group(1).upper() == "H" else "SL") if m_code_lvl else None

    # Year: 4-digit or 2-digit IB code prefix M22 / N23
    m_yr = re.search(r"(?<!\d)(20[12]\d|19\d{2})(?!\d)", clean)
    if m_yr:
        year = m_yr.group(1)
    else:
        m_code_sess = re.search(r"(?:^|[_\-.\s/])([mn])(\d{2})(?:[_\-.\s/]|$)", clean, re.I)
        year = f"20{m_code_sess.group(2)}" if m_code_sess else None

    # Session: May or November
    if re.search(r"(?:^|[_\-.\s/])(may)(?:$|[_\-.\s/])", clean, re.I):
        session = "may"
    elif re.search(r"(?:^|[_\-.\s/])(november|nov)(?:$|[_\-.\s/])", clean, re.I):
        session = "november"
    else:
        m_code_sess = re.search(r"(?:^|[_\-.\s/])([mn])(\d{2})(?:[_\-.\s/]|$)", clean, re.I)
        session = ("may" if m_code_sess.group(1).lower() == "m" else "november") if m_code_sess else None

    # Subject slug
    subj_slug = None
    for pat, slug, _ in SUBJECT_PATTERNS:
        if re.search(pat, low):
            subj_slug = slug
            break

    return {
        "subject_slug": subj_slug,
        "level": level,
        "year": year,
        "session": session,
        "paper_num": paper_num,
        "tz": tz,
        "is_markscheme": is_ms,
    }


def _effective_sig(item: dict[str, Any]) -> dict[str, Any]:
    """Parse signature from item name/title and supplement with explicit API metadata."""
    name = item.get("filename") or item.get("title") or item.get("url") or ""
    sig = parse_paper_signature(str(name))

    rtype = str(item.get("rtype") or "").lower()
    if rtype == "markscheme":
        sig["is_markscheme"] = True
    elif rtype == "paper":
        sig["is_markscheme"] = False

    if not sig["level"] and item.get("level"):
        lvl = str(item["level"]).strip().upper()
        if lvl in ("HL", "SL", "MYP"):
            sig["level"] = lvl

    if not sig["year"] and (item.get("yr") or item.get("year")):
        sig["year"] = str(item.get("yr") or item.get("year")).strip()

    if not sig["session"] and (item.get("sess") or item.get("session")):
        s_val = str(item.get("sess") or item.get("session")).strip().lower()
        sig["session"] = "may" if "may" in s_val else ("november" if "nov" in s_val else s_val)

    if not sig["tz"] and item.get("tz"):
        sig["tz"] = str(item["tz"]).strip().lower()

    return sig


def pair_papers_and_markschemes(
    items: list[dict[str, Any]],
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    """Match papers with markschemes sharing (subject_slug, level, year, session, paper_num, tz).

    Falls back to matching without tz if only one unique timezone exists in candidate markschemes.
    Decorates matched papers with `paired_markscheme: { sha256, ref, title, filename, download_url }`.
    """
    base = (base_url or get_base_url()).rstrip("/")
    ms_candidates: dict[tuple[Any, ...], list[tuple[str | None, dict[str, Any]]]] = defaultdict(list)
    item_sigs: list[dict[str, Any]] = []

    for item in items:
        sig = _effective_sig(item)
        item_sigs.append(sig)
        if sig["is_markscheme"]:
            base_key = (
                sig["subject_slug"],
                sig["level"],
                sig["year"],
                sig["session"],
                sig["paper_num"],
            )
            ms_candidates[base_key].append((sig["tz"], item))

    for idx, item in enumerate(items):
        sig = item_sigs[idx]
        rtype = str(item.get("rtype") or "").lower()
        is_paper = (rtype == "paper") or (not sig["is_markscheme"] and sig["paper_num"] is not None)

        if not is_paper:
            item.setdefault("paired_markscheme", None)
            continue

        base_key = (
            sig["subject_slug"],
            sig["level"],
            sig["year"],
            sig["session"],
            sig["paper_num"],
        )
        candidates = ms_candidates.get(base_key, [])
        matched_ms: dict[str, Any] | None = None

        # 1. Exact tz match
        for cand_tz, ms_item in candidates:
            if cand_tz == sig["tz"]:
                matched_ms = ms_item
                break

        # 2. Fall back to matching without tz if candidates have only one unique timezone
        if not matched_ms and candidates:
            unique_tzs = {cand_tz for cand_tz, _ in candidates}
            if len(unique_tzs) == 1:
                matched_ms = candidates[0][1]

        if matched_ms:
            ms_sha = matched_ms.get("sha256") or matched_ms.get("extra") or ""
            item["paired_markscheme"] = {
                "sha256": ms_sha,
                "ref": matched_ms.get("ref") or (f"ib://{ms_sha}" if ms_sha else ""),
                "title": matched_ms.get("title") or matched_ms.get("filename") or "",
                "filename": matched_ms.get("filename") or matched_ms.get("title") or "",
                "download_url": matched_ms.get("download_url") or (f"{base}/download/{ms_sha}" if ms_sha else ""),
            }
        else:
            item.setdefault("paired_markscheme", None)

    return items


def _attach_targeted_markscheme(item: dict[str, Any], base_url: str) -> None:
    """Run a targeted follow-up search for a single paper to discover its matching markscheme."""
    sig = _effective_sig(item)
    if not sig.get("subject_slug") or not sig.get("paper_num"):
        return

    subject_query = sig["subject_slug"].replace("_", " ")
    followup_q = f"{subject_query} paper {sig['paper_num']}"
    if sig.get("tz"):
        followup_q += f" {sig['tz']}"

    try:
        ms_res = search_ib(
            q=followup_q,
            rtype="markscheme",
            level=sig.get("level"),
            session=sig.get("session"),
            year=sig.get("year"),
            limit=5,
            pair_markschemes=False,
            base_url=base_url,
        )
        candidates = ms_res.get("data", [])
        if not candidates or not isinstance(candidates, list):
            return

        matching_cands: list[tuple[str | None, dict[str, Any]]] = []
        for cand in candidates:
            cand_sig = _effective_sig(cand)
            if (
                cand_sig["is_markscheme"]
                and cand_sig["subject_slug"] == sig["subject_slug"]
                and cand_sig["paper_num"] == sig["paper_num"]
                and (not sig["level"] or not cand_sig["level"] or cand_sig["level"] == sig["level"])
                and (not sig["year"] or not cand_sig["year"] or cand_sig["year"] == sig["year"])
                and (not sig["session"] or not cand_sig["session"] or cand_sig["session"] == sig["session"])
            ):
                matching_cands.append((cand_sig["tz"], cand))

        matched = None
        # 1. Exact match on tz
        for cand_tz, cand in matching_cands:
            if cand_tz == sig["tz"]:
                matched = cand
                break

        # 2. Fallback without tz if only one unique timezone
        if not matched and matching_cands:
            if len({c_tz for c_tz, _ in matching_cands}) == 1:
                matched = matching_cands[0][1]

        if matched:
            m_sha = matched.get("sha256") or matched.get("extra") or ""
            item["paired_markscheme"] = {
                "sha256": m_sha,
                "ref": matched.get("ref") or (f"ib://{m_sha}" if m_sha else ""),
                "title": matched.get("title") or matched.get("filename") or "",
                "filename": matched.get("filename") or matched.get("title") or "",
                "download_url": matched.get("download_url") or (f"{base_url}/download/{m_sha}" if m_sha else ""),
            }
    except Exception as exc:
        logger.debug("Targeted markscheme search failed for %s: %s", item.get("title"), exc)


def map_course_to_ib_params(course_name: str) -> dict[str, Any]:
    """Map Indus LMS course titles to IB catalog search parameters.

    Examples:
        'DP Physics HL' -> {'subject': 'physics', 'level': 'HL', 'query': 'physics'}
        'IB DP Mathematics: Analysis and Approaches HL' -> {'subject': 'mathematics_aa', 'level': 'HL', ...}
        'MYP Year 5 Chemistry' -> {'subject': 'chemistry', 'level': 'MYP', 'query': 'chemistry'}
        'Biology SL' -> {'subject': 'biology', 'level': 'SL', 'query': 'biology'}
    """
    if not course_name or not isinstance(course_name, str):
        return {"subject": None, "level": None, "query": ""}

    clean = course_name.strip()
    low = clean.lower()

    # Level detection: HL, SL, or MYP
    level = None
    if re.search(r"(?<![A-Za-z])HL(?![A-Za-z])", clean, re.I):
        level = "HL"
    elif re.search(r"(?<![A-Za-z])SL(?![A-Za-z])", clean, re.I):
        level = "SL"
    elif re.search(r"(?<![A-Za-z])MYP(?![A-Za-z])", clean, re.I):
        level = "MYP"

    # Subject and query extraction
    subject = None
    query = ""
    for pat, slug, q_str in SUBJECT_PATTERNS:
        if re.search(pat, low):
            subject = slug
            query = q_str
            break

    if not subject:
        # Fallback: strip common academic prefixes/grade numbers and slugify
        stripped = re.sub(r"\b(ib|dp|myp|pyp|year\s*\d+|grade\s*\d+|hl|sl)\b", "", low, flags=re.I)
        stripped = re.sub(r"[:\-_/]", " ", stripped)
        words = stripped.strip().split()
        if words:
            query = " ".join(words)
            subject = "_".join(words)

    return {
        "subject": subject,
        "level": level,
        "query": query,
    }


def search_ib(
    q: str = "",
    rtype: str | None = None,
    level: str | None = None,
    session: str | None = None,
    year: str | int | None = None,
    limit: int = 15,
    pair_markschemes: bool = True,
    *,
    tz: str | None = None,
    kind: str | None = None,
    sort: str = "rank",
    base_url: str | None = None,
) -> dict[str, Any]:
    """Search the IB resource catalog with full-text queries and metadata filters.

    Adds `ref = f"ib://{item['sha256']}"` and `download_url = f"{base_url}/download/{item['sha256']}"`
    to every returned file item.
    When `pair_markschemes=True`, pairs any past paper (`rtype == "paper"`) with its matching markscheme.
    """
    base = (base_url or get_base_url()).rstrip("/")
    cache_key = (
        "search",
        q,
        str(rtype).lower() if rtype else None,
        str(level).lower() if level else None,
        str(session).lower() if session else None,
        str(year) if year is not None else None,
        str(tz).lower() if tz else None,
        str(kind).lower() if kind else None,
        sort,
        int(limit),
        bool(pair_markschemes),
        base,
    )
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    params: dict[str, Any] = {"q": q, "limit": limit}
    if rtype:
        params["rtype"] = str(rtype).lower()
    if level:
        params["level"] = str(level).lower()
    if session:
        params["session"] = str(session).lower()
    if year is not None:
        params["year"] = str(year)
    if tz:
        params["tz"] = str(tz).lower()
    if kind:
        params["kind"] = str(kind).lower()
    if sort:
        params["sort"] = sort

    resp = requests.get(f"{base}/search", params=params, timeout=15)
    resp.raise_for_status()
    payload = resp.json()

    if isinstance(payload, dict) and "data" in payload:
        raw_items = payload["data"] if isinstance(payload["data"], list) else []
        meta = payload.get("meta", {})
        status = payload.get("status", "ok")
    elif isinstance(payload, list):
        raw_items = payload
        meta = {"count": len(raw_items)}
        status = "ok"
    else:
        raw_items = []
        meta = {}
        status = "ok"

    # Add canonical ref and download_url to every item
    for item in raw_items:
        sha = item.get("sha256") or item.get("extra") or ""
        item["sha256"] = sha
        item["ref"] = f"ib://{sha}" if sha else ""
        item["download_url"] = f"{base}/download/{sha}" if sha else ""

    # Paper ↔ markscheme pairing
    if pair_markschemes:
        pair_papers_and_markschemes(raw_items, base_url=base)
        # For any remaining unpaired papers, perform targeted follow-up search
        for item in raw_items:
            rtype_val = str(item.get("rtype") or "").lower()
            if rtype_val == "paper" and item.get("paired_markscheme") is None:
                _attach_targeted_markscheme(item, base_url=base)

    result = {
        "status": status,
        "data": raw_items,
        "meta": meta,
    }
    set_cached(cache_key, result, ttl=DEFAULT_CACHE_TTL)
    return copy.deepcopy(result)


def get_ib_facets(
    q: str = "",
    kind: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Retrieve aggregation facet counts from the IB catalog."""
    base = (base_url or get_base_url()).rstrip("/")
    cache_key = ("facets", q, kind, base)
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    params: dict[str, Any] = {"q": q}
    if kind:
        params["kind"] = kind

    resp = requests.get(f"{base}/facets", params=params, timeout=15)
    resp.raise_for_status()
    payload = resp.json()

    set_cached(cache_key, payload, ttl=DEFAULT_CACHE_TTL)
    return copy.deepcopy(payload)


def get_ib_resource(sha256: str, base_url: str | None = None) -> dict[str, Any]:
    """Retrieve metadata record for a specific resource SHA-256."""
    if not isinstance(sha256, str) or not _HEX_64_RE.fullmatch(sha256.strip()):
        raise ValueError(f"Invalid sha256 hex string: {sha256!r}")

    sha = sha256.strip().lower()
    base = (base_url or get_base_url()).rstrip("/")
    cache_key = ("resource", sha, base)
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    resp = requests.get(f"{base}/resources/{sha}", timeout=15)
    resp.raise_for_status()
    payload = resp.json()

    ref = f"ib://{sha}"
    download_url = f"{base}/download/{sha}"

    if isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], dict):
            payload["data"]["sha256"] = sha
            payload["data"]["ref"] = ref
            payload["data"]["download_url"] = download_url
        payload["sha256"] = sha
        payload["ref"] = ref
        payload["download_url"] = download_url
        result = payload
    else:
        result = {
            "status": "ok",
            "data": {
                "sha256": sha,
                "ref": ref,
                "download_url": download_url,
            },
            "meta": {},
            "sha256": sha,
            "ref": ref,
            "download_url": download_url,
        }

    set_cached(cache_key, result, ttl=DEFAULT_CACHE_TTL)
    return copy.deepcopy(result)


def get_ib_download_url(sha256: str, base_url: str | None = None) -> str:
    """Return the direct download URL for a 64-hex SHA-256."""
    if not isinstance(sha256, str) or not _HEX_64_RE.fullmatch(sha256.strip()):
        raise ValueError(f"Invalid sha256 hex string: {sha256!r}")
    sha = sha256.strip().lower()
    base = (base_url or get_base_url()).rstrip("/")
    return f"{base}/download/{sha}"


def get_ib_links(
    q: str = "",
    platform: str | None = None,
    limit: int = 20,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve outbound resource links (Google Drive, MEGA, etc.)."""
    base = (base_url or get_base_url()).rstrip("/")
    cache_key = ("links", q, platform, limit, base)
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    params: dict[str, Any] = {"q": q, "limit": limit}
    if platform:
        params["platform"] = platform

    resp = requests.get(f"{base}/links", params=params, timeout=15)
    resp.raise_for_status()
    payload = resp.json()

    if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], list):
        rows = payload["data"]
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    set_cached(cache_key, rows, ttl=DEFAULT_CACHE_TTL)
    return copy.deepcopy(rows)


def get_ib_stats(base_url: str | None = None) -> dict[str, Any]:
    """Retrieve catalog statistics and index health."""
    base = (base_url or get_base_url()).rstrip("/")
    cache_key = ("stats", base)
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    resp = requests.get(f"{base}/stats", timeout=15)
    resp.raise_for_status()
    payload = resp.json()

    set_cached(cache_key, payload, ttl=DEFAULT_CACHE_TTL)
    return copy.deepcopy(payload)
