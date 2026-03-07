#!/usr/bin/env bash
# Regenerate flatpak/python-sources.json from requirements.txt.
#
# Requires flatpak-pip-generator from:
#   https://github.com/flatpak/flatpak-builder-tools/tree/master/pip
#
# Quick setup:
#   pip install --user aiohttp aiofiles
#   curl -O https://raw.githubusercontent.com/flatpak/flatpak-builder-tools/master/pip/flatpak-pip-generator
#   chmod +x flatpak-pip-generator
#
# Then run this script from the repo root or the flatpak/ directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENERATOR="${FLATPAK_PIP_GENERATOR:-flatpak-pip-generator}"

if ! command -v "$GENERATOR" &>/dev/null; then
    echo "error: flatpak-pip-generator not found on PATH."
    echo "       See comments in this script for setup instructions."
    exit 1
fi

cd "$SCRIPT_DIR"

"$GENERATOR" \
    --runtime org.gnome.Sdk//46 \
    --output python-sources \
    --requirements-file requirements.txt

echo "Done — flatpak/python-sources.json updated."
