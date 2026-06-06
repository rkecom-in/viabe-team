"""VT-358 — shared keyword matching for the two consent-critical surfaces.

Boundary-safe CONTAINMENT (Unicode lookarounds, NFC) used by BOTH the owner DSR/opt-out gate
(`pre_filter_gate`) and the customer opt-out path (`integrations.customer_inbound`), so the two
surfaces CANNOT drift. Extracted from the VT-329 owner-gate fix.

`\\b` is DEAD for Devanagari — a matra (combining vowel sign ◌ा/◌ी, category Mc/Mn) is NOT `\\w`,
so a keyword ending in a matra (मेरा / बंद) can never anchor the trailing `\\b`; every Devanagari
pattern silently never fires. Unicode lookarounds `(?<!\\w)kw(?!\\w)` give boundary semantics that
work for BOTH scripts ("stop" still won't fire on "stopwatch"). INTENDED fail-safe over-match: a
keyword ending in a bare consonant matches THROUGH a following matra (a stem fires inside an
inflection) — conservative for consent (over-route an opt-out, never miss one).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


def nfc(body: str) -> str:
    """NFC-normalize so nukta / decomposed Devanagari matches the canonical compiled keyword."""
    return unicodedata.normalize("NFC", body or "")


def boundary_patterns(keywords: Iterable[str]) -> list[re.Pattern[str]]:
    """Compile case-insensitive, NFC, boundary-safe containment patterns — one per keyword."""
    return [
        re.compile(rf"(?<!\w){re.escape(nfc(k))}(?!\w)", re.IGNORECASE | re.UNICODE)
        for k in keywords
    ]


def contains_any(body: str, patterns: Iterable[re.Pattern[str]]) -> bool:
    """True if the NFC form of ``body`` contains ANY of the boundary patterns."""
    b = nfc(body)
    return any(p.search(b) for p in patterns)
