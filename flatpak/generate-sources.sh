#!/usr/bin/env bash
# Regenerate the Python dependency entries in python-sources.json.
#
# flatpak-builder requires included files to be single module objects, not
# arrays, so the contents of python-sources.json are inlined directly into
# io.github.cellar.json.  Use this script to regenerate python-sources.json
# when dependencies change, then manually merge the array entries into the
# manifest's "modules" section (replacing everything between the first
# python3-* module and the "cellar" module).
#
# Requires flatpak-pip-generator from:
#   https://github.com/flatpak/flatpak-builder-tools/tree/master/pip
#
# Quick setup:
#   pip install --user aiohttp aiofiles
#   curl -O https://raw.githubusercontent.com/flatpak/flatpak-builder-tools/master/pip/flatpak-pip-generator
#   chmod +x flatpak-pip-generator
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
echo "Copy the array entries into the modules section of io.github.cellar.json."
