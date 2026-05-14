import re
from typing import Iterable


def normalize(k: str) -> str:
    return k.strip().casefold()


def compile_pattern(keywords: Iterable[str]) -> "re.Pattern | None":
    # Sort by escaped length descending so longer (more specific) alternatives
    # are tried first, then by keyword text ascending for full determinism.
    # Sort by ESCAPED length (not raw) so metachar-heavy keywords like "C++"
    # rank correctly (raw len 3, escaped len 5).
    escaped = [(re.escape(k), k) for k in keywords if k]
    escaped.sort(key=lambda pair: (-len(pair[0]), pair[1]))
    union = "|".join(esc for esc, _ in escaped)
    # (?<!\w)…(?!\w) instead of \b: \b fails when a keyword ends in
    # punctuation (e.g. "C++") and the following char is also \W — no
    # \w↔\W transition, so no \b fires. Lookarounds have no such gap.
    return re.compile(rf"(?<!\w)(?:{union})(?!\w)", re.IGNORECASE) if union else None


def matches(text: str, pattern: "re.Pattern | None") -> "str | None":
    """Return the matched substring (for logging) or None."""
    if not pattern:
        return None
    m = pattern.search(text)
    return m.group(0) if m else None
