# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the IB Resource Catalog client, pairing parser, and course mapper."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import requests

from app.ib_client import (
    clear_cache,
    get_ib_download_url,
    get_ib_facets,
    get_ib_links,
    get_ib_resource,
    get_ib_stats,
    map_course_to_ib_params,
    pair_papers_and_markschemes,
    parse_paper_signature,
    search_ib,
)


@pytest.fixture(autouse=True)
def reset_ib_cache():
    """Ensure in-memory cache is clean for every test."""
    clear_cache()
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# 1. parse_paper_signature Tests
# ---------------------------------------------------------------------------


def test_parse_paper_signature_standard_conventions():
    # Paper
    p_sig = parse_paper_signature("Physics_paper_2__TZ1_HL.pdf")
    assert p_sig["subject_slug"] == "physics"
    assert p_sig["level"] == "HL"
    assert p_sig["paper_num"] == "2"
    assert p_sig["tz"] == "tz1"
    assert p_sig["is_markscheme"] is False

    # Markscheme
    ms_sig = parse_paper_signature("Physics_paper_2__TZ1_HL_markscheme.pdf")
    assert ms_sig["subject_slug"] == "physics"
    assert ms_sig["level"] == "HL"
    assert ms_sig["paper_num"] == "2"
    assert ms_sig["tz"] == "tz1"
    assert ms_sig["is_markscheme"] is True

    # May 2023 Physics HL Paper 1 MS
    m23_sig = parse_paper_signature("May 2023 Physics HL Paper 1 MS")
    assert m23_sig["subject_slug"] == "physics"
    assert m23_sig["level"] == "HL"
    assert m23_sig["year"] == "2023"
    assert m23_sig["session"] == "may"
    assert m23_sig["paper_num"] == "1"
    assert m23_sig["tz"] is None
    assert m23_sig["is_markscheme"] is True


def test_parse_paper_signature_diverse_conventions():
    # November 2021 Chemistry SL Paper 2 TZ2
    chem = parse_paper_signature("November 2021 Chemistry SL Paper 2 TZ2.pdf")
    assert chem["subject_slug"] == "chemistry"
    assert chem["level"] == "SL"
    assert chem["year"] == "2021"
    assert chem["session"] == "november"
    assert chem["paper_num"] == "2"
    assert chem["tz"] == "tz2"
    assert chem["is_markscheme"] is False

    # Mathematics Analysis and Approaches Paper 1 TZ2 SL Markscheme
    math_aa = parse_paper_signature("Mathematics_analysis_and_approaches_paper_1__TZ2_SL_markscheme.pdf")
    assert math_aa["subject_slug"] == "mathematics_aa"
    assert math_aa["level"] == "SL"
    assert math_aa["paper_num"] == "1"
    assert math_aa["tz"] == "tz2"
    assert math_aa["is_markscheme"] is True

    # Math AA abbreviated
    math_aa_abbr = parse_paper_signature("Math_AA_HL_Paper_1_TZ1.pdf")
    assert math_aa_abbr["subject_slug"] == "mathematics_aa"
    assert math_aa_abbr["level"] == "HL"
    assert math_aa_abbr["paper_num"] == "1"
    assert math_aa_abbr["tz"] == "tz1"
    assert math_aa_abbr["is_markscheme"] is False

    # Math AI abbreviated
    math_ai = parse_paper_signature("Math_AI_SL_Paper_2.pdf")
    assert math_ai["subject_slug"] == "mathematics_ai"
    assert math_ai["level"] == "SL"
    assert math_ai["paper_num"] == "2"

    # Computer Science HL
    cs = parse_paper_signature("Computer_Science_Paper_1_HL.pdf")
    assert cs["subject_slug"] == "computer_science"
    assert cs["level"] == "HL"
    assert cs["paper_num"] == "1"
    assert cs["tz"] is None
    assert cs["is_markscheme"] is False

    # Economics HL with Nov session
    econ = parse_paper_signature("2018_Nov_Economics_HL_paper_3_ms.pdf")
    assert econ["subject_slug"] == "economics"
    assert econ["level"] == "HL"
    assert econ["year"] == "2018"
    assert econ["session"] == "november"
    assert econ["paper_num"] == "3"
    assert econ["is_markscheme"] is True

    # History paper 1a
    hist = parse_paper_signature("History_paper_1a_HL.pdf")
    assert hist["subject_slug"] == "history"
    assert hist["level"] == "HL"
    assert hist["paper_num"] == "1a"
    assert hist["is_markscheme"] is False

    # Biology question paper with TZ0
    bio = parse_paper_signature("Biology_paper_1_TZ0_SL_question_paper.pdf")
    assert bio["subject_slug"] == "biology"
    assert bio["level"] == "SL"
    assert bio["paper_num"] == "1"
    assert bio["tz"] == "tz0"
    assert bio["is_markscheme"] is False

    # IB official naming scheme: M22_4_PHYSI_HP2_ENG_TZ1_XX.pdf
    ib_code = parse_paper_signature("M22_4_PHYSI_HP2_ENG_TZ1_XX.pdf")
    assert ib_code["subject_slug"] == "physics"
    assert ib_code["level"] == "HL"
    assert ib_code["year"] == "2022"
    assert ib_code["session"] == "may"
    assert ib_code["paper_num"] == "2"
    assert ib_code["tz"] == "tz1"
    assert ib_code["is_markscheme"] is False

    # IB official markscheme suffix: _M.pdf
    ib_code_ms = parse_paper_signature("M22_4_PHYSI_HP2_ENG_TZ1_XX_M.pdf")
    assert ib_code_ms["subject_slug"] == "physics"
    assert ib_code_ms["level"] == "HL"
    assert ib_code_ms["year"] == "2022"
    assert ib_code_ms["session"] == "may"
    assert ib_code_ms["paper_num"] == "2"
    assert ib_code_ms["tz"] == "tz1"
    assert ib_code_ms["is_markscheme"] is True

    # Deep folder path
    path_sig = parse_paper_signature("IB_Archive/Past_Papers/Physics/2023/May/Physics_paper_2_HL.pdf")
    assert path_sig["subject_slug"] == "physics"
    assert path_sig["year"] == "2023"
    assert path_sig["session"] == "may"
    assert path_sig["level"] == "HL"
    assert path_sig["paper_num"] == "2"


def test_parse_paper_signature_edge_cases():
    empty = parse_paper_signature("")
    assert empty["subject_slug"] is None
    assert empty["level"] is None
    assert empty["is_markscheme"] is False

    none_input = parse_paper_signature(None)  # type: ignore[arg-type]
    assert none_input["subject_slug"] is None


# ---------------------------------------------------------------------------
# 2. pair_papers_and_markschemes Tests
# ---------------------------------------------------------------------------


def test_pair_papers_and_markschemes_exact_match():
    p_sha = "a" * 64
    ms_sha = "b" * 64
    items = [
        {
            "sha256": p_sha,
            "title": "Physics_paper_2__TZ1_HL.pdf",
            "filename": "Physics_paper_2__TZ1_HL.pdf",
            "rtype": "paper",
        },
        {
            "sha256": ms_sha,
            "title": "Physics_paper_2__TZ1_HL_markscheme.pdf",
            "filename": "Physics_paper_2__TZ1_HL_markscheme.pdf",
            "rtype": "markscheme",
        },
    ]

    paired = pair_papers_and_markschemes(items, base_url="https://ib-scrape.sidevv.xyz")
    paper = paired[0]
    ms = paired[1]

    assert paper["paired_markscheme"] is not None
    assert paper["paired_markscheme"]["sha256"] == ms_sha
    assert paper["paired_markscheme"]["ref"] == f"ib://{ms_sha}"
    assert paper["paired_markscheme"]["title"] == "Physics_paper_2__TZ1_HL_markscheme.pdf"
    assert paper["paired_markscheme"]["download_url"] == f"https://ib-scrape.sidevv.xyz/download/{ms_sha}"

    # Markscheme itself does not have a paired markscheme
    assert ms["paired_markscheme"] is None


def test_pair_papers_and_markschemes_fallback_single_timezone():
    p_sha = "1" * 64
    ms_sha = "2" * 64
    # Paper without timezone specified
    items = [
        {
            "sha256": p_sha,
            "title": "Economics SL Paper 2.pdf",
            "filename": "Economics SL Paper 2.pdf",
            "rtype": "paper",
        },
        {
            "sha256": ms_sha,
            "title": "Economics SL Paper 2 TZ1 MS.pdf",
            "filename": "Economics SL Paper 2 TZ1 MS.pdf",
            "rtype": "markscheme",
        },
    ]

    paired = pair_papers_and_markschemes(items)
    paper = paired[0]
    assert paper["paired_markscheme"] is not None
    assert paper["paired_markscheme"]["sha256"] == ms_sha


def test_pair_papers_and_markschemes_conflicting_timezones_no_ambiguous_pair():
    p_sha = "1" * 64
    ms1_sha = "2" * 64
    ms2_sha = "3" * 64
    # Paper without timezone, but two candidate markschemes exist with different timezones
    items = [
        {
            "sha256": p_sha,
            "title": "Economics SL Paper 2.pdf",
            "filename": "Economics SL Paper 2.pdf",
            "rtype": "paper",
        },
        {
            "sha256": ms1_sha,
            "title": "Economics SL Paper 2 TZ1 MS.pdf",
            "filename": "Economics SL Paper 2 TZ1 MS.pdf",
            "rtype": "markscheme",
        },
        {
            "sha256": ms2_sha,
            "title": "Economics SL Paper 2 TZ2 MS.pdf",
            "filename": "Economics SL Paper 2 TZ2 MS.pdf",
            "rtype": "markscheme",
        },
    ]

    paired = pair_papers_and_markschemes(items)
    paper = paired[0]
    # Ambiguous between TZ1 and TZ2: must NOT pair
    assert paper["paired_markscheme"] is None


# ---------------------------------------------------------------------------
# 3. map_course_to_ib_params Tests
# ---------------------------------------------------------------------------


def test_map_course_to_ib_params():
    tests = [
        (
            "DP Physics HL",
            {"subject": "physics", "level": "HL", "query": "physics"},
        ),
        (
            "IB DP Mathematics: Analysis and Approaches HL",
            {"subject": "mathematics_aa", "level": "HL", "query": "mathematics analysis and approaches"},
        ),
        (
            "MYP Year 5 Chemistry",
            {"subject": "chemistry", "level": "MYP", "query": "chemistry"},
        ),
        (
            "Biology SL",
            {"subject": "biology", "level": "SL", "query": "biology"},
        ),
        (
            "IB DP Economics SL",
            {"subject": "economics", "level": "SL", "query": "economics"},
        ),
        (
            "DP Computer Science HL",
            {"subject": "computer_science", "level": "HL", "query": "computer science"},
        ),
        (
            "MYP 5 Physics",
            {"subject": "physics", "level": "MYP", "query": "physics"},
        ),
        (
            "DP History HL",
            {"subject": "history", "level": "HL", "query": "history"},
        ),
        (
            "IB DP Mathematics: Applications and Interpretation SL",
            {"subject": "mathematics_ai", "level": "SL", "query": "mathematics applications and interpretation"},
        ),
    ]

    for course_title, expected in tests:
        res = map_course_to_ib_params(course_title)
        assert res["subject"] == expected["subject"], f"Failed subject for {course_title}"
        assert res["level"] == expected["level"], f"Failed level for {course_title}"
        assert res["query"] == expected["query"], f"Failed query for {course_title}"


def test_map_course_to_ib_params_fallbacks():
    # Empty course string
    assert map_course_to_ib_params("") == {"subject": None, "level": None, "query": ""}
    assert map_course_to_ib_params(None) == {"subject": None, "level": None, "query": ""}  # type: ignore[arg-type]

    # Non-standard course
    custom = map_course_to_ib_params("DP Visual Arts HL")
    assert custom["level"] == "HL"
    assert "visual" in custom["query"]


# ---------------------------------------------------------------------------
# 4. search_ib, get_ib_resource, get_ib_facets, and Core API Tests
# ---------------------------------------------------------------------------


def _mock_response(status_code: int = 200, json_data: dict | list | None = None) -> mock.MagicMock:
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def test_search_ib_envelope_and_decorations():
    sha = "c" * 64
    fake_body = {
        "status": "ok",
        "data": [
            {
                "sha256": sha,
                "title": "Physics_paper_2__TZ1_HL.pdf",
                "filename": "Physics_paper_2__TZ1_HL.pdf",
                "rtype": "paper",
                "level": "hl",
            }
        ],
        "meta": {"count": 1, "query": "physics"},
    }

    with mock.patch("requests.get", return_value=_mock_response(200, fake_body)) as mock_get:
        # pair_markschemes=False to check plain decoration
        res = search_ib(q="physics", rtype="paper", limit=10, pair_markschemes=False)

        assert mock_get.call_count == 1
        args, kwargs = mock_get.call_args
        assert kwargs["params"]["q"] == "physics"
        assert kwargs["params"]["rtype"] == "paper"
        assert kwargs["params"]["limit"] == 10

        assert res["status"] == "ok"
        items = res["data"]
        assert len(items) == 1
        item = items[0]
        assert item["sha256"] == sha
        assert item["ref"] == f"ib://{sha}"
        assert item["download_url"] == f"https://ib-scrape.sidevv.xyz/download/{sha}"


def test_search_ib_targeted_followup_pairing():
    p_sha = "d" * 64
    ms_sha = "e" * 64

    paper_search_payload = {
        "status": "ok",
        "data": [
            {
                "sha256": p_sha,
                "title": "May 2023 Physics HL Paper 1.pdf",
                "filename": "May 2023 Physics HL Paper 1.pdf",
                "rtype": "paper",
                "level": "hl",
                "sess": "May",
                "yr": "2023",
            }
        ],
        "meta": {"count": 1},
    }

    ms_search_payload = {
        "status": "ok",
        "data": [
            {
                "sha256": ms_sha,
                "title": "May 2023 Physics HL Paper 1 MS.pdf",
                "filename": "May 2023 Physics HL Paper 1 MS.pdf",
                "rtype": "markscheme",
                "level": "hl",
                "sess": "May",
                "yr": "2023",
            }
        ],
        "meta": {"count": 1},
    }

    def side_effect(url, params=None, **kwargs):
        if params and params.get("rtype") == "markscheme":
            return _mock_response(200, ms_search_payload)
        return _mock_response(200, paper_search_payload)

    with mock.patch("requests.get", side_effect=side_effect) as mock_get:
        res = search_ib(q="May 2023 Physics", rtype="paper", pair_markschemes=True)

        assert mock_get.call_count == 2  # 1 initial search + 1 targeted follow-up search
        paper = res["data"][0]
        assert paper["paired_markscheme"] is not None
        assert paper["paired_markscheme"]["sha256"] == ms_sha
        assert paper["paired_markscheme"]["ref"] == f"ib://{ms_sha}"
        assert paper["paired_markscheme"]["title"] == "May 2023 Physics HL Paper 1 MS.pdf"


def test_get_ib_resource():
    valid_sha = "f" * 64
    fake_payload = {
        "status": "ok",
        "data": {
            "sha256": valid_sha,
            "filename": "Chemistry_HL_TZ1.pdf",
            "size": 1048576,
        },
        "meta": {},
    }

    with mock.patch("requests.get", return_value=_mock_response(200, fake_payload)):
        res = get_ib_resource(valid_sha)
        assert res["status"] == "ok"
        assert res["ref"] == f"ib://{valid_sha}"
        assert res["download_url"] == f"https://ib-scrape.sidevv.xyz/download/{valid_sha}"
        assert res["data"]["ref"] == f"ib://{valid_sha}"
        assert res["data"]["filename"] == "Chemistry_HL_TZ1.pdf"

    # Invalid sha256 validation
    with pytest.raises(ValueError, match="Invalid sha256 hex string"):
        get_ib_resource("short-sha")
    with pytest.raises(ValueError, match="Invalid sha256 hex string"):
        get_ib_resource("z" * 64)


def test_get_ib_download_url():
    valid_sha = "1" * 64
    url = get_ib_download_url(valid_sha)
    assert url == f"https://ib-scrape.sidevv.xyz/download/{valid_sha}"

    with pytest.raises(ValueError, match="Invalid sha256 hex string"):
        get_ib_download_url("invalid-hex")


def test_get_ib_facets():
    fake_facets = {
        "status": "ok",
        "data": {
            "rtype": [{"value": "paper", "count": 12000}],
            "level": [{"value": "HL", "count": 6000}],
        },
        "meta": {"query": ""},
    }

    with mock.patch("requests.get", return_value=_mock_response(200, fake_facets)) as mock_get:
        res = get_ib_facets(q="physics")
        assert mock_get.call_count == 1
        assert res["status"] == "ok"
        assert "rtype" in res["data"]


def test_get_ib_links():
    fake_links = {
        "status": "ok",
        "data": [
            {"url": "https://mega.nz/folder/xyz", "platform": "mega", "direct_file": False}
        ],
        "meta": {},
    }

    with mock.patch("requests.get", return_value=_mock_response(200, fake_links)):
        links = get_ib_links(q="drive", platform="mega", limit=10)
        assert isinstance(links, list)
        assert len(links) == 1
        assert links[0]["platform"] == "mega"


def test_get_ib_stats():
    fake_stats = {
        "status": "ok",
        "data": {
            "files": 53200,
            "bytes": 107374182400,
            "indexed": 53200,
        },
        "meta": {},
    }

    with mock.patch("requests.get", return_value=_mock_response(200, fake_stats)):
        stats = get_ib_stats()
        assert stats["status"] == "ok"
        assert stats["data"]["files"] == 53200


# ---------------------------------------------------------------------------
# 5. Caching and TTL Tests
# ---------------------------------------------------------------------------


def test_in_memory_ttl_caching_and_expiry():
    sha = "9" * 64
    fake_body = {
        "status": "ok",
        "data": [{"sha256": sha, "title": "Paper.pdf", "rtype": "notes"}],
        "meta": {},
    }

    with mock.patch("requests.get", return_value=_mock_response(200, fake_body)) as mock_get:
        # 1st call -> HTTP call
        r1 = search_ib(q="biology", pair_markschemes=False)
        assert mock_get.call_count == 1

        # 2nd call -> Hits cache, no new HTTP call
        r2 = search_ib(q="biology", pair_markschemes=False)
        assert mock_get.call_count == 1
        assert r1 == r2

        # Caller mutation safety: modifying r2 doesn't pollute r3
        r2["data"].append({"mutated": True})
        r3 = search_ib(q="biology", pair_markschemes=False)
        assert len(r3["data"]) == 1

        # clear_cache -> triggers new HTTP call
        clear_cache()
        search_ib(q="biology", pair_markschemes=False)
        assert mock_get.call_count == 2
