from __future__ import annotations

import re
from typing import Any

try:
    from config import RANK_PRUNE_PREFIXES
except Exception:
    RANK_PRUNE_PREFIXES = []


# Handles callsigns like:
# "Cobra"
# “Cobra”
# 'Cobra'
# ‘Cobra’
# This intentionally allows spaces inside the quotes, such as "Barf Bag".
QUOTE_PAIR_PATTERNS = [
    re.compile(r'"[^"\r\n]*"'),
    re.compile(r"“[^”\r\n]*”"),
    re.compile(r"'[^'\r\n]*'"),
    re.compile(r"‘[^’\r\n]*’"),
]

WHITESPACE_PATTERN = re.compile(r"\s+")


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def configured_rank_prune_prefixes() -> list[str]:
    prefixes: list[str] = []
    seen: set[str] = set()

    for raw_prefix in RANK_PRUNE_PREFIXES or []:
        prefix = clean_text(raw_prefix)

        if not prefix or prefix in seen:
            continue

        seen.add(prefix)
        prefixes.append(prefix)

    # Longest first so "CAG. ENS." is removed before "CAG".
    return sorted(prefixes, key=len, reverse=True)


def remove_quoted_callsigns(value: str) -> str:
    text = clean_text(value)

    for pattern in QUOTE_PAIR_PATTERNS:
        text = clean_text(pattern.sub(" ", text))

    return text


def remove_rank_prefixes(value: str) -> str:
    text = clean_text(value)
    prefixes = configured_rank_prune_prefixes()

    if not text or not prefixes:
        return text

    changed = True

    while changed and text:
        changed = False

        for prefix in prefixes:
            if text == prefix:
                text = ""
                changed = True
                break

            if not text.startswith(prefix):
                continue

            remainder = text[len(prefix):]

            # Require a clean boundary so LT does not remove LTJG.
            if remainder and not remainder[0].isspace() and remainder[0] not in "-–—|:/\\.":
                continue

            text = clean_text(remainder.lstrip(" -–—|:/\\."))
            changed = True
            break

    return text


def prune_display_name(value: Any, *, fallback: Any = None) -> str:
    original = clean_text(value)
    fallback_text = clean_text(fallback)

    if not original:
        return fallback_text

    # Remove callsigns before and after rank stripping.
    # This covers names like:
    # HMLA-167 XO "Cobra" Smith
    # "Cobra" HMLA-167 XO Smith
    without_quotes = remove_quoted_callsigns(original)
    pruned = remove_rank_prefixes(without_quotes)
    pruned = remove_quoted_callsigns(pruned)

    return pruned or without_quotes or original or fallback_text
