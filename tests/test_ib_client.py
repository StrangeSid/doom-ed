# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the IB Resource Catalog client, pairing parser, and course mapper."""

from __future__ import annotations

import sys
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


def _resp(payload):
    resp = mock.MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _item(sha: str, name: str, rtype: str) -> dict:
    return {"sha256": sha, "title": name, "filename": name, "rtype": rtype}


# ---------------------------------------------------------------------------
# 1. parse_paper_signature Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Physics_paper_2__TZ1_HL.pdf",
     {"subject_slug": "physics", "level": "HL", "paper_num": "2", "tz": "tz1", "is_markscheme": False}),
    ("Physics_paper_2__TZ1_HL_markscheme.pdf",
     {"subject_slug": "physics", "level": "HL", "paper_num": "2", "tz": "tz1", "is_markscheme": True}),
    ("May 2023 Physics HL Paper 1 MS",
     {"subject_slug": "physics", "level": "HL", "year": "2023", "session": "may",
      "paper_num": "1", "tz": None, "is_markscheme": True}),
])
def test_parse_paper_signature_standard_conventions(name, expected):
    assert {k: parse_paper_signature(name)[k] for k in expected} == expected


@pytest.mark.parametrize("name,expected", [
    ("November 2021 Chemistry SL Paper 2 TZ2.pdf",
     {"subject_slug": "chemistry", "level": "SL", "year": "2021", "session": "november",
      "paper_num": "2", "tz": "tz2", "is_markscheme": False}),
    ("Mathematics_analysis_and_approaches_paper_1__TZ2_SL_markscheme.pdf",
     {"subject_slug": "mathematics_aa", "level": "SL", "paper_num": "1", "tz": "tz2", "is_markscheme": True}),
    ("Math_AA_HL_Paper_1_TZ1.pdf",
     {"subject_slug": "mathematics_aa", "level": "HL", "paper_num": "1", "tz": "tz1", "is_markscheme": False}),
    ("Math_AI_SL_Paper_2.pdf",
     {"subject_slug": "mathematics_ai", "level": "SL", "paper_num": "2"}),
    ("Computer_Science_Paper_1_HL.pdf",
     {"subject_slug": "computer_science", "level": "HL", "paper_num": "1", "tz": None, "is_markscheme": False}),
    ("2018_Nov_Economics_HL_paper_3_ms.pdf",
     {"subject_slug": "economics", "level": "HL", "year": "2018", "session": "november",
      "paper_num": "3", "is_markscheme": True}),
    ("History_paper_1a_HL.pdf",
     {"subject_slug": "history", "level": "HL", "paper_num": "1a", "is_markscheme": False}),
    ("Biology_paper_1_TZ0_SL_question_paper.pdf",
     {"subject_slug": "biology", "level": "SL", "paper_num": "1", "tz": "tz0", "is_markscheme": False}),
    ("M22_4_PHYSI_HP2_ENG_TZ1_XX.pdf",
     {"subject_slug": "physics", "level": "HL", "year": "2022", "session": "may",
      "paper_num": "2", "tz": "tz1", "is_markscheme": False}),
    ("M22_4_PHYSI_HP2_ENG_TZ1_XX_M.pdf",
     {"subject_slug": "physics", "level": "HL", "year": "2022", "session": "may",
      "paper_num": "2", "tz": "tz1", "is_markscheme": True}),
    ("IB_Archive/Past_Papers/Physics/2023/May/Physics_paper_2_HL.pdf",
     {"subject_slug": "physics", "year": "2023", "session": "may", "level": "HL", "paper_num": "2"}),
])
def test_parse_paper_signature_diverse_conventions(name, expected):
    assert {k: parse_paper_signature(name)[k] for k in expected} == expected


def test_parse_paper_signature_edge_cases():
    for name in ("", None):
        sig = parse_paper_signature(name)
        assert sig["subject_slug"] is None
        assert sig["level"] is None
        assert sig["is_markscheme"] is False


# ---------------------------------------------------------------------------
# 2. pair_papers_and_markschemes Tests
# ---------------------------------------------------------------------------

def test_pair_papers_and_markschemes_exact_match():
    p_sha, ms_sha = "a" * 64, "b" * 64
    paper, ms = pair_papers_and_markschemes([
        _item(p_sha, "Physics_paper_2__TZ1_HL.pdf", "paper"),
        _item(ms_sha, "Physics_paper_2__TZ1_HL_markscheme.pdf", "markscheme"),
    ], base_url="https://ib-scrape.sidevv.xyz")

    paired = paper["paired_markscheme"]
    assert paired["sha256"] == ms_sha
    assert paired["ref"] == f"ib://{ms_sha}"
    assert paired["title"] == "Physics_paper_2__TZ1_HL_markscheme.pdf"
    assert paired["download_url"] == f"https://ib-scrape.sidevv.xyz/download/{ms_sha}"
    assert ms["paired_markscheme"] is None


def test_pair_papers_and_markschemes_fallback_single_timezone():
    ms_sha = "2" * 64
    paper = pair_papers_and_markschemes([
        _item("1" * 64, "Economics SL Paper 2.pdf", "paper"),
        _item(ms_sha, "Economics SL Paper 2 TZ1 MS.pdf", "markscheme"),
    ])[0]
    assert paper["paired_markscheme"]["sha256"] == ms_sha


def test_pair_papers_and_markschemes_conflicting_timezones_no_ambiguous_pair():
    paper = pair_papers_and_markschemes([
        _item("1" * 64, "Economics SL Paper 2.pdf", "paper"),
        _item("2" * 64, "Economics SL Paper 2 TZ1 MS.pdf", "markscheme"),
        _item("3" * 64, "Economics SL Paper 2 TZ2 MS.pdf", "markscheme"),
    ])[0]
    assert paper["paired_markscheme"] is None


# ---------------------------------------------------------------------------
# 3. map_course_to_ib_params Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("DP Physics HL", {"subject": "physics", "level": "HL", "query": "physics"}),
    ("IB DP Mathematics: Analysis and Approaches HL",
     {"subject": "mathematics_aa", "level": "HL", "query": "mathematics analysis and approaches"}),
    ("MYP Year 5 Chemistry", {"subject": "chemistry", "level": "MYP", "query": "chemistry"}),
    ("Biology SL", {"subject": "biology", "level": "SL", "query": "biology"}),
    ("IB DP Economics SL", {"subject": "economics", "level": "SL", "query": "economics"}),
    ("DP Computer Science HL", {"subject": "computer_science", "level": "HL", "query": "computer science"}),
    ("MYP 5 Physics", {"subject": "physics", "level": "MYP", "query": "physics"}),
    ("DP History HL", {"subject": "history", "level": "HL", "query": "history"}),
    ("IB DP Mathematics: Applications and Interpretation SL",
     {"subject": "mathematics_ai", "level": "SL", "query": "mathematics applications and interpretation"}),
])
def test_map_course_to_ib_params(title, expected):
    assert map_course_to_ib_params(title) == expected


def test_map_course_to_ib_params_fallbacks():
    empty = {"subject": None, "level": None, "query": ""}
    assert map_course_to_ib_params("") == empty
    assert map_course_to_ib_params(None) == empty
    custom = map_course_to_ib_params("DP Visual Arts HL")
    assert custom["level"] == "HL"
    assert "visual" in custom["query"]


# ---------------------------------------------------------------------------
# 4. search_ib, get_ib_resource, get_ib_facets, and Core API Tests
# ---------------------------------------------------------------------------

def test_search_ib_envelope_and_decorations():
    sha = "c" * 64
    body = {
        "status": "ok",
        "data": [{"sha256": sha, "title": "Physics_paper_2__TZ1_HL.pdf",
                  "filename": "Physics_paper_2__TZ1_HL.pdf", "rtype": "paper", "level": "hl"}],
        "meta": {"count": 1, "query": "physics"},
    }
    with mock.patch("requests.get", return_value=_resp(body)) as mock_get:
        res = search_ib(q="physics", rtype="paper", limit=10, pair_markschemes=False)
        params = mock_get.call_args.kwargs["params"]
        assert mock_get.call_count == 1
        assert (params["q"], params["rtype"], params["limit"]) == ("physics", "paper", 10)
        assert res["status"] == "ok" and len(res["data"]) == 1
        item = res["data"][0]
        assert item["sha256"] == sha
        assert item["ref"] == f"ib://{sha}"
        assert item["download_url"] == f"https://ib-scrape.sidevv.xyz/download/{sha}"


def test_search_ib_targeted_followup_pairing():
    p_sha, ms_sha = "d" * 64, "e" * 64
    paper_payload = {"status": "ok", "data": [{
        "sha256": p_sha, "title": "May 2023 Physics HL Paper 1.pdf", "rtype": "paper",
        "level": "hl", "sess": "May", "yr": "2023",
    }], "meta": {"count": 1}}
    ms_payload = {"status": "ok", "data": [{
        "sha256": ms_sha, "title": "May 2023 Physics HL Paper 1 MS.pdf", "rtype": "markscheme",
        "level": "hl", "sess": "May", "yr": "2023",
    }], "meta": {"count": 1}}

    def side_effect(url, params=None, **kwargs):
        return _resp(ms_payload if params and params.get("rtype") == "markscheme" else paper_payload)

    with mock.patch("requests.get", side_effect=side_effect) as mock_get:
        res = search_ib(q="May 2023 Physics", rtype="paper", pair_markschemes=True)
        assert mock_get.call_count == 2  # 1 initial search + 1 targeted follow-up search
        paired = res["data"][0]["paired_markscheme"]
        assert paired["sha256"] == ms_sha
        assert paired["ref"] == f"ib://{ms_sha}"
        assert paired["title"] == "May 2023 Physics HL Paper 1 MS.pdf"


def test_get_ib_resource():
    sha = "f" * 64
    payload = {"status": "ok", "data": {"sha256": sha, "filename": "Chemistry_HL_TZ1.pdf", "size": 1048576}, "meta": {}}
    with mock.patch("requests.get", return_value=_resp(payload)):
        res = get_ib_resource(sha)
        assert res["status"] == "ok"
        assert res["ref"] == f"ib://{sha}"
        assert res["download_url"] == f"https://ib-scrape.sidevv.xyz/download/{sha}"
        assert res["data"]["ref"] == f"ib://{sha}"
        assert res["data"]["filename"] == "Chemistry_HL_TZ1.pdf"

    for bad in ("short-sha", "z" * 64):
        with pytest.raises(ValueError, match="Invalid sha256 hex string"):
            get_ib_resource(bad)


def test_get_ib_download_url():
    sha = "1" * 64
    assert get_ib_download_url(sha) == f"https://ib-scrape.sidevv.xyz/download/{sha}"
    with pytest.raises(ValueError, match="Invalid sha256 hex string"):
        get_ib_download_url("invalid-hex")


def test_get_ib_facets():
    facets = {"status": "ok", "data": {"rtype": [{"value": "paper", "count": 12000}],
              "level": [{"value": "HL", "count": 6000}]}, "meta": {"query": ""}}
    with mock.patch("requests.get", return_value=_resp(facets)) as mock_get:
        res = get_ib_facets(q="physics")
        assert mock_get.call_count == 1
        assert res["status"] == "ok" and "rtype" in res["data"]


def test_get_ib_links():
    payload = {"status": "ok", "data": [{"url": "https://mega.nz/folder/xyz", "platform": "mega", "direct_file": False}], "meta": {}}
    with mock.patch("requests.get", return_value=_resp(payload)):
        links = get_ib_links(q="drive", platform="mega", limit=10)
    assert isinstance(links, list)
    assert len(links) == 1 and links[0]["platform"] == "mega"


def test_get_ib_stats():
    payload = {"status": "ok", "data": {"files": 53200, "bytes": 107374182400, "indexed": 53200}, "meta": {}}
    with mock.patch("requests.get", return_value=_resp(payload)):
        stats = get_ib_stats()
    assert stats["status"] == "ok"
    assert stats["data"]["files"] == 53200


# ---------------------------------------------------------------------------
# 5. Caching and TTL Tests
# ---------------------------------------------------------------------------

def test_in_memory_ttl_caching_and_expiry():
    sha = "9" * 64
    body = {"status": "ok", "data": [{"sha256": sha, "title": "Paper.pdf", "rtype": "notes"}], "meta": {}}
    with mock.patch("requests.get", return_value=_resp(body)) as mock_get:
        # 1st call -> HTTP call
        r1 = search_ib(q="biology", pair_markschemes=False)
        assert mock_get.call_count == 1
        # 2nd call -> Hits cache, no new HTTP call
        r2 = search_ib(q="biology", pair_markschemes=False)
        assert mock_get.call_count == 1 and r1 == r2
        # Caller mutation safety: modifying r2 doesn't pollute r3
        r2["data"].append({"mutated": True})
        assert len(search_ib(q="biology", pair_markschemes=False)["data"]) == 1
        # clear_cache -> triggers new HTTP call
        clear_cache()
        search_ib(q="biology", pair_markschemes=False)
        assert mock_get.call_count == 2
