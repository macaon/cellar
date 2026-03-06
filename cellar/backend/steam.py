"""Steam Store API client (no authentication required).

Provides game search and full metadata lookup via the public Steam Store API.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser

from cellar.utils.http import make_session

log = logging.getLogger(__name__)

_STORE_SEARCH = "https://store.steampowered.com/api/storesearch/"
_APP_DETAILS = "https://store.steampowered.com/api/appdetails"

_GENRE_TO_CATEGORY: dict[str, str] = {
    "Action": "Games",
    "Adventure": "Games",
    "RPG": "Games",
    "Strategy": "Games",
    "Simulation": "Games",
    "Sports": "Games",
    "Racing": "Games",
    "Casual": "Games",
    "Indie": "Games",
    "Massively Multiplayer": "Games",
    "Free to Play": "Games",
}


class SteamError(Exception):
    """Raised on Steam API errors."""


def search_games(query: str, limit: int = 10) -> list[dict]:
    """Search the Steam store by title.

    Returns a list of ``{"appid": int, "name": str}`` dicts.
    Full metadata is fetched separately via :func:`fetch_details`.
    """
    resp = make_session().get(
        _STORE_SEARCH,
        params={"term": query, "l": "english", "cc": "US"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise SteamError(f"Steam search failed ({resp.status_code})")
    items = resp.json().get("items", [])
    return [
        {"appid": item["id"], "name": item["name"]}
        for item in items[:limit]
        if "id" in item and "name" in item
    ]


def fetch_details(appid: int) -> dict:
    """Fetch full metadata for a Steam App ID.

    Returns a normalised dict with keys:
    ``appid``, ``name``, ``year``, ``developer``, ``publisher``,
    ``summary``, ``description``, ``category``, ``steam_appid``,
    ``header_image``, ``screenshots``.
    """
    resp = make_session().get(
        _APP_DETAILS,
        params={"appids": str(appid), "l": "english"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise SteamError(f"Steam appdetails failed ({resp.status_code})")
    data = resp.json()
    app_data = data.get(str(appid), {})
    if not app_data.get("success"):
        raise SteamError(f"App {appid} not found on Steam")
    return _normalise(app_data["data"])


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
    """Convert raw Steam appdetails payload to a Cellar-friendly metadata dict."""
    import datetime

    year: int | None = None
    date_str = (raw.get("release_date") or {}).get("date", "")
    if date_str:
        for fmt in ("%d %b, %Y", "%b %Y", "%Y"):
            try:
                year = datetime.datetime.strptime(date_str.strip(), fmt).year
                break
            except ValueError:
                continue

    genres = [g.get("description", "") for g in (raw.get("genres") or [])]
    category: str | None = None
    for g in genres:
        if g in _GENRE_TO_CATEGORY:
            category = _GENRE_TO_CATEGORY[g]
            break
    if category is None and genres:
        category = "Games"

    return {
        "appid": raw.get("steam_appid"),
        "name": raw.get("name", ""),
        "year": year,
        "developer": ", ".join(raw.get("developers") or []),
        "publisher": ", ".join(raw.get("publishers") or []),
        "summary": raw.get("short_description", ""),
        "description": _strip_html(raw.get("detailed_description", "")),
        "category": category,
        "steam_appid": raw.get("steam_appid"),
        "header_image": raw.get("header_image", ""),
        "screenshots": [
            s["path_full"]
            for s in (raw.get("screenshots") or [])
            if s.get("path_full")
        ],
    }
