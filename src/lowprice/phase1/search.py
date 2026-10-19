"""Serper (google.serper.dev/images) -> Candidate[].

Includes a mock mode that reads local files from work/mock_images/ so the
whole pipeline can run offline with no credentials.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import requests

from .. import config
from ..http import session

SEARCH_URL = "https://google.serper.dev/images"


@dataclass
class Candidate:
    url: str
    source: str
    width: int
    height: int
    title: str
    snippet: str


class SearchError(Exception):
    pass


def search_images(query: str, count: int = None) -> list[Candidate]:
    """Query Serper's /images endpoint for a normalized query string."""
    if not config.SERPER_API_KEY:
        raise SearchError(
            "SERPER_API_KEY not set. Use --mock for offline runs, "
            "or configure credentials per README."
        )

    count = count or config.CANDIDATES_PER_PRODUCT
    payload = json.dumps({"q": query, "num": count})
    headers = {
        "X-API-KEY": config.SERPER_API_KEY,
        "Content-Type": "application/json",
    }

    try:
        resp = session().post(
            SEARCH_URL, data=payload, headers=headers, timeout=config.SEARCH_TIMEOUT_SECONDS
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SearchError(f"search request failed: {exc}") from exc

    data = resp.json()
    items = data.get("images", [])

    candidates = []
    for item in items:
        # "domain" is the bare hostname (e.g. "en.wikipedia.org") — used directly
        # for trusted/junk/watermark domain matching. "source" is a human-readable
        # publisher name (e.g. "Wikipedia"), not a domain, so it's kept only as
        # extra scoring text, not passed through domain matching.
        candidates.append(
            Candidate(
                url=item.get("imageUrl", ""),
                source=item.get("domain", "") or item.get("link", ""),
                width=int(item.get("imageWidth", 0) or 0),
                height=int(item.get("imageHeight", 0) or 0),
                title=item.get("title", ""),
                snippet=item.get("source", ""),
            )
        )
    return candidates


def search_images_mock(query: str, count: int = None) -> list[Candidate]:
    """Offline stand-in: reads work/mock_images/index.json (list of candidate dicts).

    Falls back to enumerating image files directly if no index.json is present.
    """
    count = count or config.CANDIDATES_PER_PRODUCT
    index_path = config.MOCK_IMAGES_DIR / "index.json"

    if index_path.exists():
        raw = json.loads(index_path.read_text())
        query_words = set(query.lower().split())
        candidates = []
        for c in raw:
            haystack = f"{c.get('title', '')} {c.get('snippet', '')}".lower()
            # Mock stand-in for a real search engine's relevance ranking: keep
            # only entries that share at least one word with the query, so an
            # off-catalogue product legitimately yields zero/weak candidates.
            if query_words and not any(w in haystack for w in query_words if len(w) > 2):
                continue
            candidates.append(
                Candidate(
                    url=str(config.MOCK_IMAGES_DIR / c["file"]),
                    source=c.get("source", "mock.local"),
                    width=c.get("width", 800),
                    height=c.get("height", 800),
                    title=c.get("title", query),
                    snippet=c.get("snippet", ""),
                )
            )
        return candidates[:count]

    files = sorted(config.MOCK_IMAGES_DIR.glob("*.jpg")) + sorted(config.MOCK_IMAGES_DIR.glob("*.png"))
    candidates = [
        Candidate(
            url=str(f),
            source="mock.local",
            width=800,
            height=800,
            title=query,
            snippet="",
        )
        for f in files
    ]
    return candidates[:count]


def get_candidates(query: str, mock: bool = False, count: int = None) -> list[Candidate]:
    if mock:
        return search_images_mock(query, count)
    candidates = search_images(query, count)
    time.sleep(config.SLEEP_BETWEEN)
    return candidates
