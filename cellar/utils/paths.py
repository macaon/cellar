"""Resolve paths to data files for both development and installed contexts."""

from pathlib import Path

# When running from the source tree, data/ sits two levels above this file.
_SRC_DATA = Path(__file__).parent.parent.parent / "data"

# Flatpak / meson-installed location.
_INSTALLED_DATA = Path("/app/share/cellar")


def ui_file(name: str) -> str:
    """Return the absolute path string for a UI template file.

    Checks the source tree first so the app can be run directly with
    ``PYTHONPATH=. python3 -m cellar.main`` during development.
    """
    for candidate in (
        _SRC_DATA / "ui" / name,
        _INSTALLED_DATA / "ui" / name,
    ):
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        f"UI file '{name}' not found in {_SRC_DATA / 'ui'} or {_INSTALLED_DATA / 'ui'}"
    )
