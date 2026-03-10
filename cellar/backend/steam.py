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
_STEAM_CDN_STORE = "https://shared.steamstatic.com/store_item_assets/steam/apps"
_STEAM_CDN_COMMUNITY = "https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps"
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

    When an SGDB API key is provided the game's ``platformdata`` is used to
    construct original Steam CDN URLs (steamstatic).  Falls back to SGDB
    community assets, then to a blind Steam CDN HEAD check.

    Returns ``{"icon": url, "cover": url, "logo": url}`` — any value may
    be empty if the asset is unavailable.
    """
    result = {"icon": "", "cover": "", "logo": ""}
    session = make_session()

    if sgdb_key:
        game_id, platform_meta = _sgdb_resolve_game(session, appid, sgdb_key)
        # Primary: build URLs from Steam platform metadata
        if platform_meta:
            result = _steam_cdn_urls(appid, platform_meta)
        # Fallback: SGDB community assets for any missing slots
        if game_id:
            if not result["icon"]:
                result["icon"] = _sgdb_fetch_asset(
                    session, game_id, "icons", sgdb_key,
                    params={"styles": "official"})
            if not result["icon"]:
                result["icon"] = _sgdb_fetch_asset(
                    session, game_id, "icons", sgdb_key)
            if not result["cover"]:
                result["cover"] = _sgdb_fetch_asset(
                    session, game_id, "grids", sgdb_key,
                    params={"dimensions": "600x900"})
            if not result["logo"]:
                result["logo"] = _sgdb_fetch_asset(
                    session, game_id, "logos", sgdb_key,
                    params={"styles": "official"})
        if result["icon"] or result["cover"] or result["logo"]:
            return result

    # Last resort: blind Steam CDN HEAD check (no key needed)
    for slot, path in (("cover", "library_600x900.jpg"), ("logo", "logo.png")):
        if result[slot]:
            continue
        url = f"{_STEAM_CDN}/{appid}/{path}"
        try:
            r = session.head(url, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                result[slot] = url
        except Exception:
            pass

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


def _sgdb_resolve_game(
    session, appid: int, sgdb_key: str,
) -> tuple[int | None, dict | None]:
    """Resolve a Steam appid to a SteamGridDB game ID and platform metadata.

    Returns ``(game_id, platform_metadata)`` where *platform_metadata* is the
    Steam ``external_platform_data`` dict (contains ``clienticon``,
    ``store_asset_mtime``, ``library_capsule_full``, etc.) or ``None``.
    """
    headers = {"Authorization": f"Bearer {sgdb_key}"}
    try:
        r = session.get(
            f"{_SGDB_API}/games/steam/{appid}",
            headers=headers, params={"platformdata": "steam"}, timeout=15,
        )
    except Exception as exc:
        log.debug("SGDB game lookup request failed: %s", exc)
        return None, None
    if r.status_code != 200:
        log.debug("SGDB game lookup failed: %s %s", r.status_code, r.text[:200])
        return None, None
    data = r.json().get("data", {})
    game_id = data.get("id")
    if not game_id:
        log.debug("SGDB game lookup returned no ID for appid %s", appid)
    # Extract Steam platform metadata
    platform_meta = None
    steam_entries = (
        data.get("external_platform_data", {}).get("steam") or []
    )
    if steam_entries:
        platform_meta = steam_entries[0].get("metadata")
    return game_id, platform_meta


def _steam_cdn_urls(appid: int, meta: dict) -> dict:
    """Build original Steam CDN URLs from SGDB platform metadata."""
    result = {"icon": "", "cover": "", "logo": ""}
    mtime = meta.get("store_asset_mtime", "")
    ts = f"?t={mtime}" if mtime else ""

    # Icon — clienticon hash
    clienticon = meta.get("clienticon", "")
    if clienticon:
        result["icon"] = (
            f"{_STEAM_CDN_COMMUNITY}/{appid}/{clienticon}.ico"
        )

    # Cover — library capsule (prefer 2x)
    capsule = meta.get("library_capsule_full") or {}
    capsule_file = _first_lang_value(capsule.get("image2x") or {})
    if not capsule_file:
        capsule_file = _first_lang_value(capsule.get("image") or {})
    if capsule_file:
        result["cover"] = f"{_STEAM_CDN_STORE}/{appid}/{capsule_file}{ts}"

    # Logo — library logo (prefer 2x)
    logo = meta.get("library_logo_full") or {}
    logo_file = _first_lang_value(logo.get("image2x") or {})
    if not logo_file:
        logo_file = _first_lang_value(logo.get("image") or {})
    if logo_file:
        result["logo"] = f"{_STEAM_CDN_STORE}/{appid}/{logo_file}{ts}"

    return result


def _first_lang_value(d: dict) -> str:
    """Return the first value from a ``{"english": "file.jpg", ...}`` dict."""
    if not d:
        return ""
    # Prefer English, then whatever is first
    return d.get("english") or d.get("en") or next(iter(d.values()), "")


def _sgdb_fetch_asset(
    session, game_id: int, asset_type: str, sgdb_key: str,
    *, params: dict | None = None,
) -> str:
    """Fetch the best URL for an asset type from SteamGridDB.

    *asset_type* is one of ``"icons"``, ``"grids"``, ``"logos"``.
    """
    headers = {"Authorization": f"Bearer {sgdb_key}"}
    try:
        r = session.get(
            f"{_SGDB_API}/{asset_type}/game/{game_id}",
            headers=headers, params=params, timeout=15,
        )
    except Exception as exc:
        log.debug("SGDB %s fetch failed: %s", asset_type, exc)
        return ""
    if r.status_code != 200:
        log.debug("SGDB %s fetch failed: %s %s", asset_type, r.status_code, r.text[:200])
        return ""
    items = r.json().get("data", [])
    if not items:
        log.debug("SGDB returned no %s for game %s", asset_type, game_id)
        return ""
    # Icons: prefer .ico (multi-resolution)
    if asset_type == "icons":
        ico = [i for i in items if i.get("mime") == "image/vnd.microsoft.icon"]
        if ico:
            return ico[0]["url"]
    return items[0].get("url", "")
