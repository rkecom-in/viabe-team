"""VT-329 — PURE (no-DB) i18n matching tests for the owner pre-filter gate.

Moved OUT of test_pre_filter.py's DATABASE_URL skipif (Cowork adversarial review: Rule #15
fail-not-skip — the pure matcher must run EVERYWHERE, not only when a DB is configured). The
`dbos` import is guarded for the dep-less smoke only (not a DB gate).
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("dbos")

from orchestrator.pre_filter_gate import (  # noqa: E402
    matches_global_stop,
    matches_opt_out_or_dsr,
    matches_restart_cue,
)


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


# --- R5 / CD6 — global send-stop (tenant opt-out) vs per-customer stop (matches_global_stop) --------
@pytest.mark.parametrize(
    "body",
    [
        "bas ab message mat bhejo",  # the stop_resume turn-0 breaker
        "sabko mat bhejo",  # everyone-word scope
        "sabko message mat bhejo",
        "ab message mat bhej do",
        "dont send anyone messages",
    ],
)
def test_global_stop_fires_on_scoped_send_negation(body: str) -> None:
    # A GLOBAL send-stop is a tenant opt-out — and it must ALSO win via the shared phase-gate floor.
    assert matches_global_stop(body) is True, body
    assert matches_opt_out_or_dsr(body) is True, body


@pytest.mark.parametrize(
    "body",
    [
        # per-customer stop (Fazal CD6: suppress THAT recipient, NEVER a tenant opt-out) — falls through
        "Rajesh ko message mat bhejo",
        "us customer ko mat bhejo",
        "is customer ko mat bhejo",
        "Priya ko message mat bhejo",
        # a bare send-negation with NO global scope stays a per-CAMPAIGN reject, never a tenant opt-out
        "mat bhejo",
        "nahi bhejna",
        # a temporal hold is a DEFER, not a stop
        "abhi mat bhejna",
        # a phone/order number = a specific target, never a global stop
        "message mat bhejo 9876543210",
        # not a send-negation at all
        "message bhejo",
        "hello how are you",
        "bhej dun kya?",
    ],
)
def test_global_stop_does_not_steal_per_customer_or_bare_reject(body: str) -> None:
    assert matches_global_stop(body) is False, body


def test_global_stop_leaves_bare_reject_out_of_the_shared_floor() -> None:
    # CRITICAL money-semantics guard: a bare "mat bhejo" reply to a pending approval must NOT trip the
    # opt-out/DSR floor (it stays a per-campaign REJECT resolved by the approval classifier).
    assert matches_opt_out_or_dsr("mat bhejo") is False


def test_bare_stop_and_band_karo_routing_unchanged() -> None:
    # RULE_ORDER pin: the EN/Hinglish opt-out words are still owned by _OPT_OUT_PATTERNS, not by the
    # CD6 leg — matches_global_stop deliberately does NOT fire on them (they carry no send-negation core).
    for body in ("STOP", "band karo", "please बंद करो", "roko"):
        assert matches_global_stop(body) is False, body
        assert matches_opt_out_or_dsr(body) is True, body


# --- R5 / CD6 — opted-out RESUME cue (matches_restart_cue) ------------------------------------------
@pytest.mark.parametrize(
    "body",
    ["START", "start", "ok restart karo", "resume", "shuru karo", "chalu karo", "reactivate"],
)
def test_restart_cue_fires_on_enumerated_cues(body: str) -> None:
    assert matches_restart_cue(body) is True, body
    # A restart cue is NOT an opt-out/DSR — the resume leg requires `not matches_opt_out_or_dsr`, so a
    # repeat STOP / DSR from an opted-out tenant never gets mis-read as a resume.
    assert matches_opt_out_or_dsr(body) is False, body


@pytest.mark.parametrize(
    "body",
    ["band karo", "STOP", "restart kya?", "bhej do", "hello", "mat bhejo"],
)
def test_restart_cue_does_not_fire_on_stop_question_or_other(body: str) -> None:
    assert matches_restart_cue(body) is False, body
