"""app/research.py - Web research and content extraction tools.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import html
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any
import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def strip_html_content(html_str: str) -> tuple[str, str]:
    """Extract title and clean readable text from HTML markup.

    Strips <head>, <script>, <style>, <noscript>, and HTML comments, converts block
    elements into clean newlines, decodes HTML entities, and removes all tags.

    Returns:
        (title, readable_text)
    """
    if not html_str:
        return "", ""

    # 1. Extract title if present
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_str, re.IGNORECASE | re.DOTALL)
    title = html.unescape(title_match.group(1)).strip() if title_match else ""

    # 2. Strip <head>, <script>, <style>, <noscript>, and comments
    cleaned = re.sub(r"<head[^>]*>.*?</head>", " ", html_str, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<!--.*?-->", " ", cleaned, flags=re.DOTALL)

    # 3. Convert block level elements and breaks to newlines
    cleaned = re.sub(r"</(p|div|h[1-6]|li|tr|blockquote|section|article)>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<(br|hr)\s*/?>", "\n", cleaned, flags=re.IGNORECASE)

    # 4. Strip all remaining HTML tags
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)

    # 5. Decode HTML entities
    cleaned = html.unescape(cleaned)

    # 6. Normalize whitespace and clean empty lines
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in cleaned.splitlines()]
    readable_text = "\n".join(line for line in lines if line)

    return title, readable_text


def web_fetch(url: str, max_chars: int = 12000) -> dict[str, Any]:
    """Fetch web page content, strip script/style tags, and extract readable text.

    Args:
        url: HTTP or HTTPS URL to fetch.
        max_chars: Maximum characters of text content to return (default: 12000).

    Returns:
        dict with keys: "url", "title", "text", "status", "error"
    """
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return {
            "url": url,
            "title": "",
            "text": "",
            "status": 0,
            "error": "Invalid URL scheme (must be http:// or https://)",
        }

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    try:
        resp = requests.get(
            url,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            verify=True,
            allow_redirects=True,
        )
        status_code = resp.status_code

        if status_code >= 400:
            return {
                "url": url,
                "title": "",
                "text": "",
                "status": status_code,
                "error": f"HTTP {status_code}",
            }

        content_type = resp.headers.get("Content-Type", "").lower()
        raw_text = resp.text

        if "html" in content_type or "<html" in raw_text[:500].lower():
            title, clean_text = strip_html_content(raw_text)
        else:
            title = ""
            clean_text = raw_text.strip()

        truncated_text = clean_text[:max_chars] if max_chars > 0 else clean_text
        return {
            "url": url,
            "title": title,
            "text": truncated_text,
            "status": status_code,
            "error": None,
        }

    except requests.RequestException as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return {
            "url": url,
            "title": "",
            "text": "",
            "status": 0,
            "error": str(e),
        }
    except Exception as e:
        logger.error("Unexpected error fetching %s: %s", url, e)
        return {
            "url": url,
            "title": "",
            "text": "",
            "status": 0,
            "error": str(e),
        }


def _search_ddg_html(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Search DuckDuckGo HTML endpoint."""
    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": "https://html.duckduckgo.com/",
    }
    resp = requests.post(
        url,
        data={"q": query},
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
        verify=True,
    )
    if resp.status_code != 200:
        return []

    text = resp.text
    results = []

    matches = re.findall(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'(?:<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>|<td[^>]*class="[^"]*result-snippet[^"]*"[^>]*>(.*?)</td>)',
        text,
        re.DOTALL,
    )

    for raw_url, raw_title, snip1, snip2 in matches:
        raw_snippet = snip1 or snip2 or ""

        # Decode DDG redirect link
        if "uddg=" in raw_url:
            actual_url = urllib.parse.unquote(raw_url.split("uddg=")[1].split("&")[0])
        else:
            actual_url = raw_url

        title = html.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
        snippet = html.unescape(re.sub(r"<[^>]+>", "", raw_snippet)).strip()

        if actual_url and title:
            results.append({
                "title": title,
                "url": actual_url,
                "snippet": snippet,
            })
            if len(results) >= max_results:
                break

    return results


def _search_wikipedia_api(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Search Wikipedia JSON API fallback."""
    url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "format": "json",
        "utf8": "1",
        "srlimit": str(max_results),
    }
    headers = {"User-Agent": "doom-ed-research/1.0 (academic study coach)"}
    resp = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
        verify=True,
    )
    if resp.status_code != 200:
        return []

    data = resp.json()
    search_items = data.get("query", {}).get("search", [])
    results = []
    for item in search_items:
        title = item.get("title", "")
        page_title_encoded = urllib.parse.quote(title.replace(" ", "_"))
        item_url = f"https://en.wikipedia.org/wiki/{page_title_encoded}"
        raw_snippet = item.get("snippet", "")
        clean_snippet = html.unescape(re.sub(r"<[^>]+>", "", raw_snippet)).strip()
        results.append({
            "title": title,
            "url": item_url,
            "snippet": clean_snippet,
        })
        if len(results) >= max_results:
            break
    return results


def _search_arxiv_api(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Search arXiv Atom/XML API fallback."""
    url = "https://export.arxiv.org/api/query"
    params = {
        "search_query": f"all:{query}",
        "start": "0",
        "max_results": str(max_results),
    }
    headers = {"User-Agent": "doom-ed-research/1.0"}
    resp = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
        verify=True,
    )
    if resp.status_code != 200:
        return []

    results = []
    try:
        root = ET.fromstring(resp.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns):
            title_elem = entry.find("atom:title", ns)
            id_elem = entry.find("atom:id", ns)
            summary_elem = entry.find("atom:summary", ns)

            title = (
                re.sub(r"\s+", " ", title_elem.text).strip()
                if title_elem is not None and title_elem.text
                else ""
            )
            url = id_elem.text.strip() if id_elem is not None and id_elem.text else ""
            snippet = (
                re.sub(r"\s+", " ", summary_elem.text).strip()
                if summary_elem is not None and summary_elem.text
                else ""
            )

            if title and url:
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": snippet[:300],
                })
                if len(results) >= max_results:
                    break
    except Exception as e:
        logger.debug("Failed to parse arXiv response: %s", e)

    return results


def web_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Search the web for research topics.

    Uses DuckDuckGo HTML endpoint first, falling back to Wikipedia and arXiv APIs
    if DuckDuckGo is unavailable, ratelimited, or returns no results. Handles
    all network errors gracefully without crashing.

    Args:
        query: Search query string.
        max_results: Maximum number of search results to return (default: 5).

    Returns:
        list of dicts, each with keys: "title", "url", "snippet"
    """
    clean_query = query.strip()
    if not clean_query:
        return []

    # 1. Try DuckDuckGo HTML
    try:
        ddg_results = _search_ddg_html(clean_query, max_results=max_results)
        if ddg_results:
            return ddg_results[:max_results]
    except Exception as e:
        logger.info("DuckDuckGo search failed: %s, falling back", e)

    # 2. Fallback to Wikipedia API
    try:
        wiki_results = _search_wikipedia_api(clean_query, max_results=max_results)
        if wiki_results:
            return wiki_results[:max_results]
    except Exception as e:
        logger.info("Wikipedia fallback search failed: %s", e)

    # 3. Fallback to arXiv API
    try:
        arxiv_results = _search_arxiv_api(clean_query, max_results=max_results)
        if arxiv_results:
            return arxiv_results[:max_results]
    except Exception as e:
        logger.info("arXiv fallback search failed: %s", e)

    return []
