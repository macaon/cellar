"""Lutris.net API client for game metadata lookup.

Provides game search and metadata fetch via the public Lutris REST API
(no authentication required).  Results are normalised to the same dict
format used by :mod:`cellar.backend.steam` so that consumers can treat
all metadata sources uniformly.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser

from cellar.utils.http import make_session

log = logging.getLogger(__name__)

_API_BASE = "https://lutris.net/api/games"

_GENRE_TO_CATEGORY: dict[str, str] = {
    "Action": "Games",
    "Adventure": "Games",
    "RPG": "Games",
    "Role-playing": "Games",
    "Strategy": "Games",
    "Simulation": "Games",
    "Sports": "Games",
    "Racing": "Games",
    "Casual": "Games",
    "Indie": "Games",
    "Puzzle": "Games",
    "Shooter": "Games",
    "Platform": "Games",
    "Fighting": "Games",
    "Arcade": "Games",
}


class LutrisError(Exception):
    """Raised on Lutris API errors."""


def search_games(query: str, limit: int = 10) -> list[dict]:
    """Search the Lutris catalogue by title.

    Returns a list of dicts with keys: ``name``, ``slug``, ``year``,
    ``cover``, ``provider_games``.
    """
    resp = make_session().get(
        _API_BASE,
        params={"search": query},
        timeout=15,
    )
    if resp.status_code != 200:
        raise LutrisError(f"Lutris search failed ({resp.status_code})")
    data = resp.json()
    results = data.get("results", [])
    out: list[dict] = []
    for item in results[:limit]:
        out.append({
            "name": item.get("name", ""),
            "slug": item.get("slug", ""),
            "year": item.get("year"),
            "cover": item.get("coverart") or item.get("banner_url") or "",
            "provider_games": item.get("provider_games") or [],
        })
    return out


def fetch_details(slug: str) -> dict:
    """Fetch full metadata for a Lutris game by slug.

    Returns a normalised dict with the same keys as
    :func:`cellar.backend.steam.fetch_details`.
    """
    resp = make_session().get(f"{_API_BASE}/{slug}", timeout=15)
    if resp.status_code != 200:
        raise LutrisError(f"Lutris detail fetch failed ({resp.status_code})")
    return _normalise(resp.json())


def extract_provider_id(
    provider_games: list[dict], service: str,
) -> str | None:
    """Extract a cross-reference ID from a Lutris ``provider_games`` list.

    >>> extract_provider_id([{"service": "gog", "slug": "123"}], "gog")
    '123'
    """
    for entry in provider_games:
        if entry.get("service") == service:
            return entry.get("slug")
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    """Strip HTML tags, returning plain text with collapsed whitespace."""
    class _Stripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            self.parts.append(data)

    stripper = _Stripper()
    stripper.feed(html)
    return re.sub(r"\s+", " ", "".join(stripper.parts)).strip()


def _normalise(raw: dict) -> dict:
    """Convert Lutris API game detail to a Cellar-friendly metadata dict."""
    genres = [
        g.get("name", "") for g in (raw.get("genres") or [])
        if g.get("name")
    ]

    category: str | None = None
    for g in genres:
        if g in _GENRE_TO_CATEGORY:
            category = _GENRE_TO_CATEGORY[g]
            break
    if category is None and genres:
        category = "Games"

    description = raw.get("description") or ""
    if description:
        description = _strip_html(description)

    steam_appid: int | None = None
    raw_steam = raw.get("steamid")
    if raw_steam:
        try:
            steam_appid = int(raw_steam)
        except (ValueError, TypeError):
            pass

    provider_games = raw.get("provider_games") or []

    return {
        "name": raw.get("name", ""),
        "year": raw.get("year"),
        "developer": "",  # Lutris does not provide developer/publisher
        "publisher": "",
        "summary": "",
        "description": description,
        "category": category,
        "steam_appid": steam_appid,
        "website": raw.get("website") or "",
        "genres": genres,
        "header_image": raw.get("coverart") or raw.get("banner_url") or "",
        "screenshots": [],  # Lutris API does not return screenshots
        "lutris_slug": raw.get("slug", ""),
        "provider_games": provider_games,
    }
