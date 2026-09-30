"""Candidate spans for Jev value extraction.

Jev doesn't generate text, so free-form values (a song name, a search query, a site) are
extracted by selection: code proposes every plausible span of the request, Jev picks the one
that is the value (or "none"). The result is always a verbatim piece of what the user said.
"""

from __future__ import annotations

import re

NONE = "none"
_EDGE_PUNCT = "\"'“”‘’.,!?;:()[]"


def tokenize(text: str) -> list[str]:
    tokens = (t.strip(_EDGE_PUNCT) for t in re.findall(r"\S+", text))
    return [t for t in tokens if t]


def candidate_spans(text: str, max_len: int = 10, limit: int = 240) -> list[str]:
    """Contiguous word spans up to `max_len` words, plus every suffix (long queries usually
    run to the end of the sentence). Deduplicated, in document order, capped for Jev's
    255-option limit (leaving room for the no-match option)."""
    tokens = tokenize(text)
    n = len(tokens)
    seen: set[str] = set()
    spans: list[str] = []

    def add(span: str) -> None:
        if span and span.lower() != NONE and span not in seen:
            seen.add(span)
            spans.append(span)

    for i in range(n):
        for j in range(i + 1, min(n, i + max_len) + 1):
            add(" ".join(tokens[i:j]))
        add(" ".join(tokens[i:]))
    return spans[:limit]
