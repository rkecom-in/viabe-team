"""VT-329 — PURE (no-DB) i18n matching tests for the owner pre-filter gate.

Moved OUT of test_pre_filter.py's DATABASE_URL skipif (Cowork adversarial review: Rule #15
fail-not-skip — the pure matcher must run EVERYWHERE, not only when a DB is configured). The
`dbos` import is guarded for the dep-less smoke only (not a DB gate).
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("dbos")

from orchestrator.pre_filter_gate import matches_opt_out_or_dsr  # noqa: E402


def test_devanagari_dsr_matches() -> None:
    """The Devanagari DSR keyword now fires — it was 100% DEAD under `\\b` (matras ∉ `\\w`, so a
    keyword ending in a matra could never anchor the trailing `\\b`)."""
    assert matches_opt_out_or_dsr("मेरा डेटा हटाओ") is True
    assert matches_opt_out_or_dsr("refund मेरा डेटा delete") is True
    assert matches_opt_out_or_dsr("hello how are you") is False  # benign, no over-fire


def test_mixed_script_dsr_matches() -> None:
    """Cowork: owners code-switch mid-sentence. A complete keyword (any script) anywhere routes,
    AND the now-curated code-switched keywords ('data delete' / 'delete karo') cover the
    script-SPLIT phrase that was the BLOCK miss ('मेरा data delete karo')."""
    assert matches_opt_out_or_dsr("मेरा डेटा delete karo") is True
    assert matches_opt_out_or_dsr("मेरा data delete karo") is True  # was the adversarial BLOCK miss
    assert matches_opt_out_or_dsr("mera data delete karo") is True
    assert matches_opt_out_or_dsr("data delete karo refund") is True


def test_opt_out_containment_matches() -> None:
    """Cowork: opt-out is boundary-safe CONTAINMENT now (was whole-body-exact, which missed
    'please बंद करो' / danda variants) — Hinglish 'band karo'/'roko' + EN STOP all route."""
    assert matches_opt_out_or_dsr("band karo") is True
    assert matches_opt_out_or_dsr("please बंद करो") is True
    assert matches_opt_out_or_dsr("STOP") is True
    assert matches_opt_out_or_dsr("roko ye") is True
    assert matches_opt_out_or_dsr("hi how are you") is False


def test_devanagari_stem_through_matra_is_intended_failsafe() -> None:
    """A keyword ending in a bare consonant matches THROUGH a following matra (matras ∉ `\\w` →
    `(?!\\w)` passes) — a stem 'हट' fires inside 'हटाओ'. INTENDED fail-safe over-match for
    DSR/opt-out (over-route a deletion/opt-out request, never miss one; covers inflections). A
    following CONSONANT (`\\w`) still blocks it — no runaway match into an unrelated word."""
    pat = re.compile(r"(?<!\w)हट(?!\w)", re.IGNORECASE | re.UNICODE)
    assert pat.search("हटाओ") is not None  # stem-through-matra — by design
    assert pat.search("हटक") is None  # क is \w → boundary holds
