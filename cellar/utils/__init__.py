"""Shared utility helpers."""

from __future__ import annotations

import re

_NATURAL_RE = re.compile(r"(\d+)")


def natural_sort_key(s: str) -> list[str | int]:
    """Return a sort key that orders embedded numbers numerically.

    >>> natural_sort_key("Kathy Rain 2: Soothsayer")
    ['kathy rain ', 2, ': soothsayer']
    >>> natural_sort_key("Kathy Rain: Director's Cut")
    ["kathy rain: director's cut"]
    """
    parts: list[str | int] = []
    for i, tok in enumerate(_NATURAL_RE.split(s.lower())):
        if i % 2 == 1:
            parts.append(int(tok))
        elif tok:
            parts.append(tok)
    return parts
