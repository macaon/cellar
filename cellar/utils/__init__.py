"""Shared utility helpers."""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"(\d+|[a-zA-Z]+)")


def natural_sort_key(s: str) -> list[tuple[int, int | str]]:
    """Return a sort key that orders embedded numbers numerically.

    Splits on word/number boundaries so punctuation and whitespace are
    ignored.  Text tokens sort before numeric tokens at the same position,
    so "Kathy Rain: Director's Cut" (text at pos 3) sorts before
    "Kathy Rain 2: Soothsayer" (number at pos 3).

    >>> natural_sort_key("Game 2") < natural_sort_key("Game 10")
    True
    >>> k = natural_sort_key
    >>> k("Kathy Rain: Director's Cut") < k("Kathy Rain 2: Soothsayer")
    True
    """
    result: list[tuple[int, int | str]] = []
    for tok in _TOKEN_RE.findall(s):
        if tok.isdigit():
            result.append((1, int(tok)))
        else:
            result.append((0, tok.lower()))
    return result
