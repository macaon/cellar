"""IGDB (Internet Game Database) API client.

Requires a Twitch Developer application Client ID + Secret.
See https://api-docs.igdb.com/

The bearer token is cached in config.json and auto-refreshed when it
expires (or within one hour of expiry).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from cellar.utils.http import make_session

log = logging.getLogger(__name__)

_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_API_BASE = "https://api.igdb.com/v4"

# Map IGDB genre names → Cellar category.  Any recognised game genre maps to
# "Games"; non-game genres are intentionally absent (None is returned).
_GENRE_TO_CATEGORY: dict[str, str] = {
    "Shooter": "Games",
    "Role-playing (RPG)": "Games",
    "Strategy": "Games",
    "Fighting": "Games",
    "Simulator": "Games",
    "Sport": "Games",
    "Adventure": "Games",
    "Arcade": "Games",
    "Racing": "Games",
    "Music": "Games",
    "Platform": "Games",
    "Puzzle": "Games",
    "Real Time Strategy (RTS)": "Games",
    "Turn-based strategy (TBS)": "Games",
    "Hack and slash/Beat 'em up": "Games",
    "Pinball": "Games",
    "Card & Board Game": "Games",
    "MOBA": "Games",
    "Point-and-click": "Games",
    "Visual Novel": "Games",
    "Indie": "Games",
}


class IGDBError(Exception):
    """Raised on IGDB API or network errors."""


class IGDBClient:
    """Twitch/IGDB API client with automatic bearer-token management.

    Parameters
    ----------
    client_id:
        Twitch application Client ID.
    client_secret:
        Twitch application Client Secret.
    """

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str = ""
        self._token_expiry: datetime = datetime.min.replace(tzinfo=timezone.utc)

        # Warm up from the cached token stored in config.
        from cellar.backend import config

        creds = config.load_igdb_creds()
        if creds and creds.get("token") and creds.get("token_expiry"):
            try:
                expiry = datetime.fromisoformat(creds["token_expiry"])
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry > datetime.now(timezone.utc) + timedelta(hours=1):
                    self._token = creds["token"]
                    self._token_expiry = expiry
            except (ValueError, KeyError):
                pass

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _ensure_token(self) -> str:
        """Return a valid bearer token, requesting a new one if necessary."""
        if self._token and datetime.now(timezone.utc) + timedelta(hours=1) < self._token_expiry:
            return self._token

        resp = make_session().post(
            _TOKEN_URL,
            params={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            raise IGDBError(
                f"Token request failed ({resp.status_code}): {resp.text[:200]}"
            )

        data = resp.json()
        self._token = data["access_token"]
        expires_in: int = data.get("expires_in", 3600)
        self._token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        from cellar.backend import config

        config.save_igdb_token(self._token, self._token_expiry.isoformat())
        return self._token

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    def search_games(self, query: str, limit: int = 10) -> list[dict]:
        """Search IGDB for games matching *query*.

        Returns a list of normalised dicts with keys:
        ``id``, ``name``, ``year``, ``developer``, ``publisher``,
        ``summary``, ``cover_image_id``, ``category``, ``steam_appid``.
        """
        token = self._ensure_token()
        session = make_session()
        headers = {
            "Client-ID": self._client_id,
            "Authorization": f"Bearer {token}",
        }

        # Step 1: full-text search — get basic fields + IDs.
        # external_games is a back-referenced relationship that IGDB's search
        # command does not reliably expand, so we fetch it separately.
        search_body = (
            f'search "{_escape(query)}"; '
            "fields name,first_release_date,"
            "involved_companies.developer,involved_companies.publisher,"
            "involved_companies.company.name,"
            "summary,genres.name,cover.image_id; "
            f"limit {limit};"
        )
        resp = session.post(
            f"{_API_BASE}/games",
            headers=headers,
            data=search_body,
            timeout=15,
        )
        if resp.status_code != 200:
            raise IGDBError(
                f"IGDB search failed ({resp.status_code}): {resp.text[:200]}"
            )

        games = resp.json()
        if not games:
            return []

        # Step 2: fetch external_games by game ID — reliable when using `where`.
        ids = ",".join(str(g["id"]) for g in games if "id" in g)
        if ids:
            ext_body = (
                f"where id = ({ids}); "
                "fields external_games.uid,external_games.category; "
                f"limit {limit};"
            )
            ext_resp = session.post(
                f"{_API_BASE}/games",
                headers=headers,
                data=ext_body,
                timeout=15,
            )
            if ext_resp.status_code == 200:
                ext_by_id = {g["id"]: g for g in ext_resp.json() if "id" in g}
                for g in games:
                    gid = g.get("id")
                    if gid in ext_by_id:
                        g["external_games"] = ext_by_id[gid].get("external_games", [])

        return [_normalise(g) for g in games]

    def fetch_cover(self, image_id: str) -> bytes:
        """Download the cover image for *image_id* and return raw JPEG bytes."""
        url = (
            f"https://images.igdb.com/igdb/image/upload/t_cover_big/{image_id}.jpg"
        )
        resp = make_session().get(url, timeout=30)
        if resp.status_code != 200:
            raise IGDBError(f"Cover download failed ({resp.status_code})")
        return resp.content


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _escape(s: str) -> str:
    """Escape double-quotes inside an Apicalypse string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _normalise(raw: dict) -> dict:
    """Convert a raw IGDB game object to a simplified Cellar-friendly dict."""
    # Release year from Unix timestamp
    ts = raw.get("first_release_date")
    year: int | None = datetime.utcfromtimestamp(ts).year if ts else None

    # Developer / Publisher from involved_companies
    developers: list[str] = []
    publishers: list[str] = []
    for ic in raw.get("involved_companies") or []:
        company_name = (ic.get("company") or {}).get("name", "")
        if not company_name:
            continue
        if ic.get("developer"):
            developers.append(company_name)
        if ic.get("publisher"):
            publishers.append(company_name)

    # Genre → Cellar category
    genre_names = [g.get("name", "") for g in (raw.get("genres") or [])]
    category: str | None = None
    for g in genre_names:
        if g in _GENRE_TO_CATEGORY:
            category = _GENRE_TO_CATEGORY[g]
            break
    if category is None and genre_names:
        category = "Games"  # unknown genre, but still a game

    cover_id: str | None = (raw.get("cover") or {}).get("image_id") or None

    # Steam App ID from external_games (category 1 = Steam)
    steam_appid: int | None = None
    for eg in raw.get("external_games") or []:
        if eg.get("category") == 1:
            try:
                steam_appid = int(eg["uid"])
            except (KeyError, ValueError, TypeError):
                pass
            break

    return {
        "id": raw.get("id"),
        "name": raw.get("name", ""),
        "year": year,
        "developer": ", ".join(developers),
        "publisher": ", ".join(publishers),
        "summary": raw.get("summary", ""),
        "cover_image_id": cover_id,
        "category": category,
        "steam_appid": steam_appid,
    }
