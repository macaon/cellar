"""Steam Store API client and SteamGridDB asset fetcher.

Provides game search and full metadata lookup via the public Steam Store API,
plus high-res icon/cover/logo downloads via the SteamGridDB API (optional key).
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser

from cellar.utils.http import make_session

log = logging.getLogger(__name__)

_STORE_SEARCH = "https://store.steampowered.com/api/storesearch/"
_APP_DETAILS = "https://store.steampowered.com/api/appdetails"

_STEAM_CDN = "https://cdn.akamai.steamstatic.com/steam/apps"
_SGDB_API = "https://www.steamgriddb.com/api/v2"

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

    genres = [g.get("description", "") for g in (raw.get("genres") or []) if g.get("description")]
    category: str | None = None
    for g in genres:
        if g in _GENRE_TO_CATEGORY:
            category = _GENRE_TO_CATEGORY[g]
            break
    if category is None and genres:
        category = "Games"

    # about_the_game is cleaner (no external images/links) than detailed_description
    about = raw.get("about_the_game") or ""
    detailed = raw.get("detailed_description") or ""
    description = _strip_html(about) if about else _strip_html(detailed)

    return {
        "appid": raw.get("steam_appid"),
        "name": raw.get("name", ""),
        "year": year,
        "developer": ", ".join(raw.get("developers") or []),
        "publisher": ", ".join(raw.get("publishers") or []),
        "summary": raw.get("short_description", ""),
        "description": description,
        "category": category,
        "steam_appid": raw.get("steam_appid"),
        "website": raw.get("website") or "",
        "genres": genres,
        "header_image": raw.get("header_image", ""),
        "screenshots": [
            {"thumbnail": s["path_thumbnail"], "full": s["path_full"]}
            for s in (raw.get("screenshots") or [])
            if s.get("path_thumbnail") and s.get("path_full")
        ],
    }


# ---------------------------------------------------------------------------
# Steam image asset fetcher (CDN + SteamGridDB)
# ---------------------------------------------------------------------------

def fetch_steam_images(appid: int, sgdb_key: str = "") -> dict:
    """Return download URLs for icon, cover, and logo for a Steam app.

    *Cover* and *logo* use predictable Steam CDN URLs (no key needed).
    *Icon* (.ico, up to 256×256) requires a SteamGridDB API key; without
    one the ``icon`` value will be empty.

    Returns ``{"icon": url, "cover": url, "logo": url}`` — any value may
    be empty if the asset is unavailable.
    """
    result = {"icon": "", "cover": "", "logo": ""}
    session = make_session()

    # Cover — Steam library capsule (600×900 vertical)
    cover_url = f"{_STEAM_CDN}/{appid}/library_600x900.jpg"
    try:
        r = session.head(cover_url, timeout=10, allow_redirects=True)
        if r.status_code == 200:
            result["cover"] = cover_url
    except Exception:
        pass

    # Logo — transparent PNG from Steam CDN
    logo_url = f"{_STEAM_CDN}/{appid}/logo.png"
    try:
        r = session.head(logo_url, timeout=10, allow_redirects=True)
        if r.status_code == 200:
            result["logo"] = logo_url
    except Exception:
        pass

    # Icon — SteamGridDB (official style, .ico preferred)
    if sgdb_key:
        try:
            result["icon"] = _sgdb_fetch_icon(session, appid, sgdb_key)
        except Exception as exc:
            log.debug("SteamGridDB icon lookup failed: %s", exc)

    return result


def download_steam_image(url: str, dest: str, sgdb_key: str = "") -> str:
    """Download an image URL to *dest* path.  Returns the path on success."""
    session = make_session()
    headers = {}
    if sgdb_key and _SGDB_API in url:
        headers["Authorization"] = f"Bearer {sgdb_key}"
    resp = session.get(url, headers=headers, timeout=30, stream=True)
    resp.raise_for_status()
    from pathlib import Path
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)
    return dest


def _sgdb_fetch_icon(session, appid: int, sgdb_key: str) -> str:
    """Look up the best .ico icon via SteamGridDB."""
    headers = {"Authorization": f"Bearer {sgdb_key}"}

    # Resolve Steam appid → SteamGridDB game ID
    r = session.get(
        f"{_SGDB_API}/games/steam/{appid}",
        headers=headers, timeout=15,
    )
    if r.status_code != 200:
        log.debug("SGDB game lookup failed: %s %s", r.status_code, r.text[:200])
        return ""
    game_id = r.json().get("data", {}).get("id")
    if not game_id:
        log.debug("SGDB game lookup returned no ID for appid %s", appid)
        return ""

    # Fetch icons (any style)
    r = session.get(
        f"{_SGDB_API}/icons/game/{game_id}",
        headers=headers,
        timeout=15,
    )
    if r.status_code != 200:
        log.debug("SGDB icon fetch failed: %s %s", r.status_code, r.text[:200])
        return ""

    icons = r.json().get("data", [])
    if not icons:
        log.debug("SGDB returned no icons for game %s (appid %s)", game_id, appid)
        return ""
    # Prefer .ico files (multi-resolution), fall back to .png
    ico_icons = [i for i in icons if i.get("mime") == "image/vnd.microsoft.icon"]
    if ico_icons:
        return ico_icons[0]["url"]
    return icons[0].get("url", "")
