"""GOG DB metadata client.

Fetches game metadata from the public GOG DB JSON data files
(no authentication required).  GOG DB has no search API — a GOG product
ID must be known beforehand (typically obtained from a Lutris cross-reference).

Image hashes are resolved to full CDN URLs via the GOG static CDN.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser

from cellar.utils.http import make_session

log = logging.getLogger(__name__)

_GOGDB_DATA = "https://www.gogdb.org/data/products"
_CDN = "https://images.gog-statics.com"

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
    "Point-and-click": "Games",
}


class GogDbError(Exception):
    """Raised on GOG DB fetch errors."""


def fetch_details(gog_id: str) -> dict:
    """Fetch full metadata for a GOG product ID from GOG DB.

    Returns a normalised dict with the same keys as
    :func:`cellar.backend.steam.fetch_details`.
    """
    url = f"{_GOGDB_DATA}/{gog_id}/product.json"
    resp = make_session().get(url, timeout=15)
    if resp.status_code == 404:
        raise GogDbError(f"GOG product {gog_id} not found")
    if resp.status_code != 200:
        raise GogDbError(f"GOG DB fetch failed ({resp.status_code})")
    return _normalise(resp.json(), gog_id)


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


def _parse_global_date(date_str: str | None) -> int | None:
    """Extract year from a GOG DB ``global_date`` ISO 8601 string.

    >>> _parse_global_date("1995-11-01T00:00:00+02:00")
    1995
    >>> _parse_global_date(None)
    """
    if not date_str or len(date_str) < 4:
        return None
    try:
        return int(date_str[:4])
    except (ValueError, TypeError):
        return None


def _image_url(hash_str: str, ext: str = "jpg") -> str:
    """Build a full GOG CDN URL from an image hash."""
    return f"{_CDN}/{hash_str}.{ext}" if hash_str else ""


def _normalise(raw: dict, gog_id: str) -> dict:
    """Convert GOG DB product.json to a Cellar-friendly metadata dict."""
    # Tags (genres)
    tags = raw.get("tags") or []
    if isinstance(tags, list):
        genres = [t.get("name", t) if isinstance(t, dict) else str(t) for t in tags]
    else:
        genres = []

    category: str | None = None
    for g in genres:
        if g in _GENRE_TO_CATEGORY:
            category = _GENRE_TO_CATEGORY[g]
            break
    if category is None and genres:
        category = "Games"

    # Developer / publisher
    developers = raw.get("developers") or []
    dev_names = [
        d.get("name", d) if isinstance(d, dict) else str(d)
        for d in developers
    ]
    publisher_raw = raw.get("publisher")
    if isinstance(publisher_raw, dict):
        publisher = publisher_raw.get("name", "")
    elif isinstance(publisher_raw, str):
        publisher = publisher_raw
    else:
        publisher = ""

    # Description
    description = raw.get("description") or ""
    if description:
        description = _strip_html(description)

    # Images
    icon_hash = raw.get("image_icon") or ""
    logo_hash = raw.get("image_logo") or ""
    boxart_hash = raw.get("image_boxart") or ""
    bg_hash = raw.get("image_background") or ""
    header = _image_url(boxart_hash) if boxart_hash else _image_url(bg_hash)

    # Screenshots
    screenshot_hashes = raw.get("screenshots") or []
    screenshots = [
        {
            "thumbnail": _image_url(h, "webp").replace(
                ".webp", "_product_card_v2_thumbnail_271.webp",
            ),
            "full": _image_url(h, "webp"),
        }
        for h in screenshot_hashes
        if h
    ]

    # Steam cross-ref (GOG DB sometimes includes this)
    steam_appid: int | None = None

    return {
        "name": raw.get("title") or raw.get("name") or "",
        "year": _parse_global_date(raw.get("global_date")),
        "developer": ", ".join(dev_names),
        "publisher": publisher,
        "summary": "",  # GOG DB has description but no separate summary
        "description": description,
        "category": category,
        "steam_appid": steam_appid,
        "website": "",
        "genres": genres,
        "header_image": header,
        "icon_image": _image_url(icon_hash) if icon_hash else "",
        "logo_image": _image_url(logo_hash) if logo_hash else "",
        "screenshots": screenshots,
        "gog_id": gog_id,
    }
