"""Finds a query in OCR lines the way OCR errs. Port of TextMatching.swift: case-, accent- and
width-insensitive, blind to whitespace (recognition drops and adds spaces), characters UI fonts make
look alike count as the same (l I | 1, O 0, ß B, bracket and quote shapes), and a few other errors are
tolerated (one per five characters). Exact matches win; approximate ones are only returned when there
is no exact one."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class TextMatch:
    """A place where a query was found in a recognized line."""

    line_index: int
    #: The matched characters: line[start:end] (code point indices into the original text).
    start: int
    end: int
    #: Edit distance to the query after folding (0 = same text ignoring case and accents).
    errors: int


#: Characters OCR confuses in UI fonts (after case folding, so "I" arrives as "i").
_LOOK_ALIKES = {
    "|": "l", "1": "l", "i": "l", "!": "l", "ı": "l",
    "0": "o",
    "ss": "b",  # "ß" folds to "ss"
    "{": "(", "[": "(", "}": ")", "]": ")",
    "“": "\"", "”": "\"", "„": "\"", "‘": "'", "’": "'", "‚": "'",
    "`": "'", "´": "'",
    "–": "-", "—": "-", "‐": "-", "−": "-",
}


def token(character: str) -> str:
    """One comparable unit per character (a base character plus its combining marks)."""
    decomposed = unicodedata.normalize("NFKD", character)
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()
    if not folded:  # a lone combining mark
        folded = character
    return _LOOK_ALIKES.get(folded, folded)


def _clusters(line: str) -> list[tuple[int, int]]:
    """(start, end) of each non-whitespace character, combining marks kept with their base."""
    clusters: list[tuple[int, int]] = []
    for index, char in enumerate(line):
        if unicodedata.combining(char) and clusters and clusters[-1][1] == index:
            clusters[-1] = (clusters[-1][0], index + 1)
        elif not char.isspace():
            clusters.append((index, index + 1))
    return clusters


def find(query: str, lines: list[str], limit: int = 20) -> list[TextMatch]:
    needle = [token(line[start:end]) for line in [query] for start, end in _clusters(line)]
    if not needle:
        return []
    allowed_errors = len(needle) // 5
    matches: list[TextMatch] = []
    for index, line in enumerate(lines):
        positions = _clusters(line)
        haystack = [token(line[start:end]) for start, end in positions]
        for start, end, errors in search(needle, haystack, allowed_errors):
            matches.append(TextMatch(index, positions[start][0], positions[end - 1][1], errors))
    if any(match.errors == 0 for match in matches):
        matches = [match for match in matches if match.errors == 0]
    matches.sort(key=lambda match: (match.errors, match.line_index))
    return matches[:limit]


def search(needle: list[str], haystack: list[str], max_errors: int) -> list[tuple[int, int, int]]:
    """Approximate substring search (Sellers): every non-overlapping place in `haystack` within
    `max_errors` edits of `needle`, best first per region. (start, end exclusive, errors) in tokens."""
    m, n = len(needle), len(haystack)
    if m == 0 or n == 0:
        return []
    # Column-wise DP over the haystack; `start` tracks where each alignment began.
    previous_cost = list(range(m + 1))
    previous_start = [0] * (m + 1)
    candidates: list[tuple[int, int, int]] = []
    for j in range(1, n + 1):
        cost = [0] * (m + 1)
        start = [j] * (m + 1)
        for i in range(1, m + 1):
            substitution = previous_cost[i - 1] + (0 if needle[i - 1] == haystack[j - 1] else 1)
            skip_needle = cost[i - 1] + 1
            skip_haystack = previous_cost[i] + 1
            if substitution <= skip_needle and substitution <= skip_haystack:
                cost[i] = substitution
                start[i] = j - 1 if i == 1 else previous_start[i - 1]
            elif skip_haystack <= skip_needle:
                cost[i] = skip_haystack
                start[i] = previous_start[i]
            else:
                cost[i] = skip_needle
                start[i] = start[i - 1]
        if cost[m] <= max_errors and start[m] < j:
            candidates.append((start[m], j, cost[m]))
        previous_cost = cost
        previous_start = start
        previous_start[0] = j
    # Keep the best of overlapping candidates: fewest errors, then closest to the query length.
    ranked = sorted(candidates, key=lambda c: (c[2], abs((c[1] - c[0]) - m), c[0]))
    chosen: list[tuple[int, int, int]] = []
    for candidate in ranked:
        if not any(other[0] < candidate[1] and candidate[0] < other[1] for other in chosen):
            chosen.append(candidate)
    return sorted(chosen, key=lambda c: c[0])
