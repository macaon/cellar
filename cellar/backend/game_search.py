"""Unified multi-source game metadata search.

Fans out to Steam and Lutris in parallel, promotes Lutris results that
have a GOG cross-reference, deduplicates, and returns a merged list.
When the user picks a result, full details are fetched from the
appropriate backend — with GOG DB enrichment when available.

All detail dicts returned are compatible with the Steam result format
so that consumers (:func:`_apply_lookup_result`) work unchanged.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One row in the unified game picker."""

    name: str
    source: str        # "Steam", "GOG", "Lutris"
    source_id: str     # appid (str), gog_id, or slug
    year: int | None
    subtitle: str      # e.g. "Steam · App ID 440"
    fetch_key: tuple[str, str]  # (backend, id) for dispatch


def search_all(query: str, limit: int = 10) -> list[SearchResult]:
    """Search Steam and Lutris in parallel, return merged results.

    Lutris results that have a GOG cross-reference are promoted to
    source ``"GOG"`` (GOG DB has richer metadata).  Duplicates across
    sources are removed (Steam-preferred).
    """
    steam_results: list[SearchResult] = []
    lutris_results: list[SearchResult] = []

    def _search_steam() -> list[SearchResult]:
        from cellar.backend.steam import fuzzy_search_games, SteamError
        try:
            raw = fuzzy_search_games(query, limit)
        except SteamError as exc:
            log.warning("Steam search failed: %s", exc)
            return []
        return [
            SearchResult(
                name=r["name"],
                source="Steam",
                source_id=str(r["appid"]),
                year=None,
                subtitle=f"Steam \u00b7 App ID {r['appid']}",
                fetch_key=("steam", str(r["appid"])),
            )
            for r in raw
        ]

    def _search_lutris() -> list[SearchResult]:
        from cellar.backend.lutris import (
            search_games, extract_provider_id, LutrisError,
        )
        try:
            raw = search_games(query, limit)
        except LutrisError as exc:
            log.warning("Lutris search failed: %s", exc)
            return []
        out: list[SearchResult] = []
        for r in raw:
            gog_id = extract_provider_id(r.get("provider_games", []), "gog")
            if gog_id:
                out.append(SearchResult(
                    name=r["name"],
                    source="GOG",
                    source_id=gog_id,
                    year=r.get("year"),
                    subtitle=f"GOG \u00b7 {gog_id}",
                    fetch_key=("gog", gog_id),
                ))
            else:
                out.append(SearchResult(
                    name=r["name"],
                    source="Lutris",
                    source_id=r.get("slug", ""),
                    year=r.get("year"),
                    subtitle=f"Lutris \u00b7 {r.get('slug', '')}",
                    fetch_key=("lutris", r.get("slug", "")),
                ))
        return out

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(_search_steam): "steam",
            pool.submit(_search_lutris): "lutris",
        }
        for future in as_completed(futures, timeout=15):
            tag = futures[future]
            try:
                results = future.result()
            except Exception:
                log.exception("Search failed for %s", tag)
                continue
            if tag == "steam":
                steam_results = results
            else:
                lutris_results = results

    deduped = _deduplicate(steam_results, lutris_results)
    return (steam_results + deduped)[:limit * 2]


def fetch_details(result: SearchResult) -> dict:
    """Fetch full metadata for a picked search result.

    Dispatches to the appropriate backend based on ``result.fetch_key``.
    For GOG results, fetches from GOG DB directly.  For Lutris results,
    fetches from Lutris and enriches with GOG DB when a cross-reference
    is available.

    Returns a dict compatible with ``_apply_lookup_result()``.
    """
    backend, id_ = result.fetch_key

    if backend == "steam":
        from cellar.backend.steam import fetch_details as steam_fetch
        return steam_fetch(int(id_))

    if backend == "gog":
        from cellar.backend.gogdb import fetch_details as gog_fetch
        return gog_fetch(id_)

    if backend == "lutris":
        from cellar.backend.lutris import (
            fetch_details as lutris_fetch,
            extract_provider_id,
        )
        detail = lutris_fetch(id_)
        gog_id = extract_provider_id(
            detail.get("provider_games", []), "gog",
        )
        if gog_id:
            return _enrich_with_gog(detail, gog_id)
        return detail

    log.error("Unknown search backend: %s", backend)
    return {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _deduplicate(
    steam_results: list[SearchResult],
    lutris_results: list[SearchResult],
) -> list[SearchResult]:
    """Remove Lutris/GOG results that duplicate a Steam result.

    Uses rapidfuzz when available (ratio >= 90), otherwise falls back
    to exact case-insensitive match.
    """
    if not steam_results or not lutris_results:
        return lutris_results

    steam_names = {r.name.lower().strip() for r in steam_results}

    try:
        from rapidfuzz import fuzz

        def _is_dup(name: str) -> bool:
            name_lower = name.lower().strip()
            return any(
                fuzz.ratio(name_lower, sn) >= 90 for sn in steam_names
            )
    except ImportError:
        def _is_dup(name: str) -> bool:
            return name.lower().strip() in steam_names

    return [r for r in lutris_results if not _is_dup(r.name)]


def _enrich_with_gog(lutris_detail: dict, gog_id: str) -> dict:
    """Merge GOG DB metadata into a Lutris detail dict.

    GOG DB fields fill empty Lutris fields.  Year *always* prefers GOG's
    ``global_date`` when available.  Returns a new dict (no mutation).
    """
    from cellar.backend.gogdb import fetch_details as gog_fetch, GogDbError

    try:
        gog = gog_fetch(gog_id)
    except (GogDbError, Exception) as exc:
        log.warning("GOG DB enrichment failed for %s: %s", gog_id, exc)
        return lutris_detail

    merged = dict(lutris_detail)

    # Year: always prefer GOG global_date
    if gog.get("year"):
        merged["year"] = gog["year"]

    # Fill empty fields from GOG
    for key in ("developer", "publisher", "description", "summary",
                "website", "header_image", "icon_image", "logo_image"):
        if not merged.get(key) and gog.get(key):
            merged[key] = gog[key]

    # Genres: prefer GOG if Lutris has none
    if not merged.get("genres") and gog.get("genres"):
        merged["genres"] = gog["genres"]
        merged["category"] = gog.get("category")

    # Screenshots: append GOG screenshots
    gog_screenshots = gog.get("screenshots") or []
    existing = merged.get("screenshots") or []
    if gog_screenshots:
        merged["screenshots"] = list(existing) + gog_screenshots

    # Carry over GOG ID
    merged["gog_id"] = gog_id

    return merged
