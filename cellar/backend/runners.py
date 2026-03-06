"""GE-Proton runner management via the GitHub Releases API.

Replaces ``backend/components.py`` (dulwich-based bottlesdevs/components sync).
GE-Proton is the only supported runner family for Cellar + umu-launcher.

``fetch_releases`` queries the GitHub Releases API once per hour (cached in
memory).  ``installed_runners`` lists runners already present on disk.
``is_installed`` is a quick directory-existence check.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

_RELEASES_URL = (
    "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases"
)
_CACHE_TTL = 3600.0  # one hour; safely within GitHub's 60 req/hr unauthenticated limit

_cache: tuple[float, list[dict]] | None = None


def fetch_releases(limit: int = 20) -> list[dict]:
    """Return recent GE-Proton releases from the GitHub Releases API.

    Each dict has keys:
    - ``name``     — display name (str)
    - ``tag``      — git tag (str), e.g. ``"GE-Proton10-32"``
    - ``url``      — download URL for the ``.tar.gz`` (str)
    - ``size``     — archive size in bytes (int)
    - ``checksum`` — SHA512 checksum URL prefixed with ``"sha512:"`` (str);
                     empty string if unavailable

    Response is cached in memory for one hour.  On network failure the
    previous cache (if any) is returned rather than raising.
    """
    global _cache
    now = time.monotonic()
    if _cache is not None:
        age, releases = _cache
        if now - age < _CACHE_TTL:
            return releases[:limit]

    from cellar.utils.http import make_session

    try:
        session = make_session()
        resp = session.get(
            _RELEASES_URL,
            params={"per_page": limit},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to fetch GE-Proton releases: %s", exc)
        return _cache[1][:limit] if _cache else []

    releases: list[dict] = []
    for rel in resp.json():
        tag = rel.get("tag_name", "")
        name = rel.get("name", "") or tag
        assets = rel.get("assets", [])
        for asset in assets:
            aname: str = asset.get("name", "")
            # The main tarball; skip checksum sidecar files.
            if aname.endswith(".tar.gz") and not aname.endswith(".sha512sum"):
                # Look for a SHA512 checksum sidecar asset.
                checksum = ""
                for other in assets:
                    if other.get("name", "") == f"{aname}.sha512sum":
                        checksum = "sha512:" + other.get("browser_download_url", "")
                        break
                releases.append({
                    "name": name,
                    "tag": tag,
                    "url": asset.get("browser_download_url", ""),
                    "size": asset.get("size", 0),
                    "checksum": checksum,
                })
                break  # one tarball per release

    _cache = (now, releases)
    return releases[:limit]


def get_release_info(runner_name: str) -> dict | None:
    """Return the release dict for *runner_name* (matched by tag), or None.

    *runner_name* is the directory name used on disk, which corresponds to the
    git tag (e.g. ``"GE-Proton10-32"``).
    """
    for rel in fetch_releases():
        if rel["tag"] == runner_name or rel["name"] == runner_name:
            return rel
    return None


def is_installed(runner_name: str) -> bool:
    """True if *runner_name* directory exists in ``runners_dir()``."""
    from cellar.backend.umu import runners_dir
    return (runners_dir() / runner_name).is_dir()


def installed_runners() -> list[str]:
    """Names of all runners in ``runners_dir()``, newest-first (lexicographic desc)."""
    from cellar.backend.umu import runners_dir
    rdir = runners_dir()
    try:
        return sorted(
            (d.name for d in rdir.iterdir() if d.is_dir()),
            reverse=True,
        )
    except OSError:
        return []


def remove_runner(runner_name: str) -> None:
    """Delete a runner directory from disk."""
    import shutil
    from cellar.backend.umu import runners_dir
    target = runners_dir() / runner_name
    if target.is_dir():
        shutil.rmtree(target)
        log.info("Removed runner %s", runner_name)
