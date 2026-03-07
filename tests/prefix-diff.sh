#!/usr/bin/env bash
# prefix-diff.sh — compare two prefix snapshots produced by prefix-snapshot.sh
#
# Usage:
#   ./prefix-diff.sh <before.tsv> <after.tsv>
#
# Output sections:
#   NEW      — files created by Wine/Proton on first launch
#   MODIFIED — files whose size or mtime changed
#   DELETED  — files present before but gone after

set -euo pipefail

BEFORE="${1:?Usage: $0 <before.tsv> <after.tsv>}"
AFTER="${2:?Usage: $0 <before.tsv> <after.tsv>}"

for f in "$BEFORE" "$AFTER"; do
    [[ -f "$f" ]] || { echo "Error: '$f' not found" >&2; exit 1; }
done

# TSV columns: path  size  mtime
# join output for matched lines: path  size_b  mtime_b  size_a  mtime_a

echo "=== NEW (created on first launch) ==="
join -t$'\t' -j1 -v2 "$BEFORE" "$AFTER" | cut -f1
NEW_COUNT=$(join -t$'\t' -j1 -v2 "$BEFORE" "$AFTER" | wc -l)

echo ""
echo "=== MODIFIED (size or mtime changed) ==="
join -t$'\t' -j1 "$BEFORE" "$AFTER" \
    | awk -F'\t' '{ if ($2 != $4 || int($3) != int($5)) printf "%s\n  size:  %s -> %s\n  mtime: %s -> %s\n", $1, $2, $4, $3, $5 }'
MOD_COUNT=$(join -t$'\t' -j1 "$BEFORE" "$AFTER" \
    | awk -F'\t' '$2 != $4 || int($3) != int($5)' | wc -l)

echo ""
echo "=== DELETED ==="
join -t$'\t' -j1 -v1 "$BEFORE" "$AFTER" | cut -f1
DEL_COUNT=$(join -t$'\t' -j1 -v1 "$BEFORE" "$AFTER" | wc -l)

echo ""
echo "--- Summary ---"
echo "  New:      $NEW_COUNT"
echo "  Modified: $MOD_COUNT"
echo "  Deleted:  $DEL_COUNT"
echo "  Before total: $(wc -l < "$BEFORE")  After total: $(wc -l < "$AFTER")"
