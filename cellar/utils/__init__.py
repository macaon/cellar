"""Shared utility helpers."""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"(\d+|[a-zA-Z]+)")
_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)


def natural_sort_key(s: str) -> tuple[int, list[str]]:
    """Return a sort key that orders titles naturally.

    Rules:
    1. Titles starting with numbers sort before titles starting with
       letters (``"20XX"`` before ``"Amnesia"``).
    2. At subsequent positions, text sorts before numbers so the
       original title precedes its numbered sequel
       (``"Kathy Rain: Director's Cut"`` before ``"Kathy Rain 2"``).
    3. Leading articles ("The", "A", "An") are stripped so titles
       sort by their significant word.

    >>> natural_sort_key("Game 2") < natural_sort_key("Game 10")
    True
    >>> natural_sort_key("60 Seconds") < natural_sort_key("Game")
    True
    >>> natural_sort_key("The Witcher") < natural_sort_key("Xenoblade")
    True
    >>> k = natural_sort_key
    >>> k("Kathy Rain: Director's Cut") < k("Kathy Rain 2: Soothsayer")
    True
    """
    s = _ARTICLE_RE.sub("", s)
    tokens = _TOKEN_RE.findall(s)

    # First token determines the top-level group: 0 = starts with
    # digit (sort first), 1 = starts with letter.
    starts_num = 0 if tokens and tokens[0].isdigit() else 1

    parts: list[str] = []
    for tok in tokens:
        if tok.isdigit():
            # "1:" prefix → numbers sort after text at the same position.
            parts.append(f"1:{tok.zfill(20)}")
        else:
            parts.append(f"0:{tok.lower()}")
    return (starts_num, parts)
