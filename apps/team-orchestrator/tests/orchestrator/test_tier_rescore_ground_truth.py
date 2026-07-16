"""VT-640 instrument fix — tier_rescore.py seed-aware GROUND TRUTH block (no Anthropic API call).

tier_rescore.py imports the `anthropic` SDK lazily (inside `_client()`), so its pure ground-truth
renderers are exercisable dep-less. These pin the seed-awareness that stops the blind sonnet judge
false-flagging harness-seeded facts (the reconnect_broken_sync connector-health false-positive) as
fabrication.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CANARIES = Path(__file__).resolve().parents[2] / "canaries"
sys.path.insert(0, str(_CANARIES))

import tier_rescore as tr  # noqa: E402 — after the sys.path insert


def test_seed_lapsed_renders_healthy_connector_ground_truth():
    # VT-640 — `--seed-lapsed-customers N` ALSO seeds a HEALTHY google_sheet connector + verified
    # GST/ownership (convo_harness _seed_lapsed_customers). The block must tell the judge so a grounded
    # "connected / recently-synced / not seeing a break" honest report is NOT flagged as fabrication.
    block = tr._render_ground_truth_block(
        {"setup_args": ["--onboarded", "--seed-lapsed-customers", "6"]}
    )
    assert block is not None
    assert "HEALTHY 'google_sheet' connector" in block
    assert "last synced just now" in block
    # the fabrication boundary: only a made-up ACTION the DB doesn't back is a breaker
    assert "reconnected" in block.lower() and "fixed the sync" in block.lower()
    assert "do NOT flag it as fabrication" in block
    # the seed-count line is still present (both facts render)
    assert "POOL of 6 customers" in block


def test_no_seed_lapsed_no_connector_ground_truth():
    # No --seed-lapsed-customers and no --journey → nothing factual to inject → None (unchanged).
    assert tr._render_ground_truth_block({"setup_args": ["--onboarded", "--flow", "ready_asked"]}) is None
    assert tr._render_ground_truth_block({"setup_args": []}) is None


def test_journey_draft_still_renders_without_connector_line():
    # A --journey scenario (no --seed-lapsed-customers) still gets the Chennai/type draft line, and
    # must NOT gain a spurious connector-health line (no connector was seeded).
    block = tr._render_ground_truth_block({"setup_args": ["--journey", "--draft-city", "Pune"]})
    assert block is not None
    assert "city='Pune'" in block
    assert "google_sheet' connector" not in block


def test_vt641_ground_truth_grounds_recovery_rupee_estimate():
    """VT-641 instrument — the seed writes a per-customer past-order amount, so a recovery ₹ range
    derived from order sizes must be declared GROUNDED (kills the ₹250-750 fabrication FP)."""
    block = tr._render_ground_truth_block(
        {"setup_args": ["--onboarded", "--seed-lapsed-customers", "8"]}
    )
    assert block is not None
    assert "past order" in block.lower()
    assert "recovery" in block.lower() and "grounded" in block.lower()


def test_vt641_render_transcript_dedups_relisted_sids():
    """VT-641 instrument — the late-reply-sweep re-lists turns with IDENTICAL message_sids; the
    rendered transcript must show each real message ONCE (kills the loop_stall FP)."""
    entry = {
        "name": "j_x",
        "steps": [
            {"transcript": [
                {"role": "owner", "text": "kitne lapsed?", "message_sid": "SMa"},
                {"role": "assistant", "text": "6 lapsed.", "message_sid": "MKb"},
                {"role": "system", "text": "[internal route: none]", "message_sid": None},
                {"role": "owner", "text": "draft banao", "message_sid": "SMc"},
                {"role": "assistant", "text": "drafted.", "message_sid": "MKd"},
                # late-reply-sweep re-lists turn 1 with the SAME sids (the artifact):
                {"role": "owner", "text": "kitne lapsed?", "message_sid": "SMa"},
                {"role": "assistant", "text": "6 lapsed.", "message_sid": "MKb"},
            ]}
        ],
    }
    rendered = tr.render_transcript_for_judge(entry)
    assert rendered.count("6 lapsed.") == 1, rendered
    assert rendered.count("kitne lapsed?") == 1, rendered
    # a GENUINE new message (distinct sid) is preserved
    assert rendered.count("drafted.") == 1


def test_vt641_render_transcript_keeps_genuine_distinct_repeat():
    """A real duplicate emission carries a DIFFERENT sid, so a genuine loop_stall is still surfaced."""
    entry = {
        "name": "j_y",
        "steps": [
            {"transcript": [
                {"role": "assistant", "text": "6 lapsed.", "message_sid": "MK1"},
                {"role": "owner", "text": "aur?", "message_sid": "SM2"},
                {"role": "assistant", "text": "6 lapsed.", "message_sid": "MK3"},
            ]}
        ],
    }
    rendered = tr.render_transcript_for_judge(entry)
    assert rendered.count("6 lapsed.") == 2, rendered


# --- CL-2026-07-16 money_action demotion (DB is the sole Tier-1 money authority) -----------------


def _verdict(scenario: str, breaker_classes: list[str], quality_acceptable: bool) -> tr.TranscriptVerdict:
    return tr.TranscriptVerdict(
        scenario=scenario,
        trust_breakers=[tr.TrustBreaker(category=c, quote="", why="") for c in breaker_classes],
        quality_acceptable=quality_acceptable,
        quality_reason="",
    )


def test_money_action_is_tier2_demoted_not_tier1():
    """Fazal CL-2026-07-16: the LLM ``money_action`` is DEMOTED to Tier-2 (DB asserts are the sole
    Tier-1 money authority). A transcript whose ONLY breaker is money_action must NOT count as a
    Tier-1 breaker, but must STILL be visible — folded into the Tier-2 quality population (a signal
    kept, not deleted)."""
    v = _verdict("j01_winback", ["money_action"], quality_acceptable=False)
    assert v.has_trust_breaker() is True  # the breaker is still recorded (signal kept)
    assert v.has_tier1_breaker() is False  # but it is NOT a Tier-1 breaker
    agg = tr.aggregate_tiers([v], [])
    assert agg["tier1_breaker_count"] == 0
    assert agg["tier1_ok"] is True
    # demoted INTO the quality denominator: it is the sole clean transcript, quality_acceptable False
    assert agg["tier1_clean_count"] == 1
    assert agg["tier2_fraction"] == 0.0
    # and the breaker text is still rendered in the per-scenario detail
    assert agg["scenarios"][0]["trust_breakers"][0]["class"] == "money_action"


def test_non_money_breaker_still_tier1():
    """A NON-demoted breaker (e.g. fabrication) is unaffected — still a hard Tier-1 miss, and its
    transcript is excluded from the Tier-2 quality denominator."""
    v = _verdict("j09_fab", ["fabrication"], quality_acceptable=False)
    assert v.has_tier1_breaker() is True
    agg = tr.aggregate_tiers([v], [])
    assert agg["tier1_breaker_count"] == 1
    assert agg["tier1_ok"] is False
    assert agg["tier1_clean_count"] == 0
    assert agg["tier2_fraction"] is None  # no clean transcripts to measure quality over


def test_mixed_money_and_real_breaker_stays_tier1():
    """A transcript carrying BOTH money_action AND a real breaker is still Tier-1 (the real breaker
    dominates) — demotion never rescues a transcript that has a non-demoted breaker."""
    v = _verdict("j_x", ["money_action", "fabrication"], quality_acceptable=False)
    assert v.has_tier1_breaker() is True
    agg = tr.aggregate_tiers([v], [])
    assert agg["tier1_breaker_count"] == 1
