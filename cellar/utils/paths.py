"""Resolve paths to data files for both development and installed contexts."""

from pathlib import Path

# When running from the source tree, data/ sits two levels above this file.
_SRC_DATA = Path(__file__).parent.parent.parent / "data"

# Flatpak / meson-installed location.
_INSTALLED_DATA = Path("/app/share/cellar")


def icons_dir() -> str:
    """Return the directory to register with the icon theme search path.

    The directory contains a ``hicolor/`` subtree with bundled symbolic icons.
    Checked in the source tree first so ``PYTHONPATH=. python3 -m cellar.main``
    works without a build step.
    """
    for candidate in (_SRC_DATA / "icons", _INSTALLED_DATA / "icons"):
        if candidate.is_dir():
            return str(candidate)
    return str(_SRC_DATA / "icons")


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
