#!/usr/bin/env bash
# prefix-snapshot.sh — record mtime+size for every file/symlink in a prefix
#
# Usage:
#   ./prefix-snapshot.sh <prefix_dir> [output_file]
#
# Run once on a fresh prefix, launch the app, run again, then diff with:
#   ./prefix-diff.sh before.tsv after.tsv

set -euo pipefail

PREFIX="${1:?Usage: $0 <prefix_dir> [output_file]}"
OUTPUT="${2:-snapshot-$(date +%Y%m%d-%H%M%S).tsv}"

if [[ ! -d "$PREFIX" ]]; then
    echo "Error: '$PREFIX' is not a directory" >&2
    exit 1
fi

find "$PREFIX" \( -type f -o -type l \) -printf '%P\t%s\t%T@\n' \
    | sort -k1,1 \
    > "$OUTPUT"

echo "Snapshot written: $OUTPUT"
echo "Entries:          $(wc -l < "$OUTPUT")"
