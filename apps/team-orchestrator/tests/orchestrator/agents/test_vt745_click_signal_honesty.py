"""VT-745 — the click signal has no feed yet, and this test keeps that statement honest.

**RETRACTION, 2026-08-15.** This file previously said the signal was *"unobtainable through any channel
this product has"*. Fazal's question — "isn't that exchange inside the 24h window?" — showed that was
too absolute (ruling D-B, CL-2026-08-15-three-m2b-rulings): an IN-SESSION exchange has an open window,
so a free-form message needs no template and could carry a tracked link today. "Unobtainable" was
wrong. What is true is narrower and worse-sounding than a channel limitation:

  1. No customer-audience template declares a URL/link variable, so the COLD win-back — the case the
     tier rule is actually about — has nowhere to put a link. (`trial_subscribe_link`, the only
     link-bearing template, is audience=owner.) That template is Fazal-side, off the critical path.
  2. **No production surface composes a customer-facing link at all.** Both customer send paths send
     TEMPLATES, and the in-session replies in `integrations.customer_inbound` are three fixed
     sentences. The in-session capability D-B identified is real; no feature uses it.
  3. **The link-bearing CHANNELS are themselves unwired.** `/r/<token>` is an EMAIL/SMS acquisition
     primitive (`integrations/hook_channels.py`, VT-288) and nothing calls those either. Sent to a
     customer already in WhatsApp it redirects them to where they already are.

So the mint has no honest caller today, and manufacturing one would be the SIXTH
built-exported-and-called-by-nothing this fortnight — the defect class this row was opened to prevent.
The infrastructure is complete and waiting; what is missing is a customer-facing link worth sending,
which is a product feature rather than a wiring gap. The honest deliverable is therefore the shortfall
note at the point of the read — plus this test, so the note cannot rot.

THE SHAPE, and why it is not a permanently-red test. A test that just asserted "the gap is open" would
poison the green baseline and put an unearned failure in front of the promotion gate. This follows the
VT-669 registry-honesty pattern instead: **GREEN while the gap is open and the note describes it, and
AUTO-RED the moment reality changes and the note does not.** Two triggers, matching the two findings —
a production caller for the mint appears, or a customer template gains a link variable.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[3] / "src" / "orchestrator"
_FREQ = _SRC / "agents" / "send_frequency.py"
_CONFIG = Path(__file__).resolve().parents[3] / "config" / "twilio_templates.yaml"


def _production_mint_callers() -> list[str]:
    """Every production CALL of `mint_customer_hook_link`, found by AST rather than by grep.

    Scans `src/` only — a test calling the mint is expected and proves nothing about production, which
    is the exact distinction VT-745 was created by (a function built, exported, and called by nothing).

    AST, not text matching, because the first version of this scan flagged a DOCSTRING in
    `hook_links.py` that merely names the function. A grep cannot tell a call from prose about a call,
    and a false red here would be worse than useless: it would train the next reader to ignore the
    gate. `ast.Call` sees only real invocations, and it needs no per-file exclusions — so a caller
    added inside the defining module is still caught, which a "skip hook_links.py" shortcut would have
    missed. That shortcut is exactly the scan-narrowing the row warns against.
    """
    hits: list[str] = []
    for path in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover — a syntax error is another test's problem
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if name == "mint_customer_hook_link":
                hits.append(f"{path.relative_to(_SRC)}:{node.lineno}")
    return hits


def _customer_templates_with_a_link_variable() -> list[str]:
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_CONFIG.read_text())
    entries = data.get("templates", data) if isinstance(data, dict) else {}
    out: list[str] = []
    for name, entry in (entries or {}).items():
        if not isinstance(entry, dict) or entry.get("audience") != "customer":
            continue
        variables = entry.get("variables") or []
        if any(("link" in str(v).lower() or "url" in str(v).lower()) for v in variables):
            out.append(f"{name} vars={variables}")
    return out


def test_the_shortfall_note_is_still_at_the_point_of_the_read():
    """The note is the deliverable. Deleting it re-hides a degraded ratified rule — and the original
    row instructed "delete the shortfall note when the mint lands", so an unguarded note is one
    well-intentioned cleanup away from gone while the gap is still open."""
    text = _FREQ.read_text()
    assert "THE CLICK SIGNAL IS NOT FED YET (VT-745)" in text, (
        "the click-signal shortfall note was removed from send_frequency.py while the gap is still "
        "open — a reader now sees a three-signal tier rule that evaluates two"
    )
    # D-B 2026-08-15: "unobtainable" is retracted, so the evidence tokens move with the claim. Each
    # of these is one of the three findings a future reader needs in order NOT to re-roster "just
    # wire the mint" — which is still the thing that does not work, for a more specific reason.
    for evidence in ("audience=owner", "hook_channels", "no honest caller"):
        assert evidence in text, (
            f"the note lost its {evidence!r} evidence — without it the next reader will re-roster "
            "'just wire the mint', which is the thing that does not work"
        )


def test_no_production_caller_mints_a_customer_hook_link_YET():
    """AUTO-RED TRIGGER 1. Green while nothing mints. The moment a production path does, this fails and
    forces the note (and the tier documentation) to be corrected — because at that point `clicked` IS
    fed and the two-signal caveat becomes the lie."""
    callers = _production_mint_callers()
    assert not callers, (
        "a production path now mints customer hook links:\n  "
        + "\n  ".join(callers)
        + "\n\nThe click signal may now be REAL. Re-verify end-to-end (a send produces a "
        "customer_hook_links row, a click stamps last_clicked_at, and a clicked-not-replied customer "
        "reaches Tier A), then DELETE the VT-745 shortfall note in send_frequency.py and this test. "
        "Do not silence this by narrowing the scan."
    )


def test_no_customer_template_can_carry_a_link_YET():
    """AUTO-RED TRIGGER 2 — the finding the row did not have. Even a perfect mint caller cannot deliver
    a link while no customer template has a URL variable. If one appears, the blocker is gone and the
    note's central claim is obsolete."""
    with_links = _customer_templates_with_a_link_variable()
    assert not with_links, (
        "a customer-audience template now declares a link/url variable:\n  "
        + "\n  ".join(with_links)
        + "\n\nThat removes VT-745's real blocker — a tracked link now has somewhere to go. Wire "
        "mint_customer_hook_link into that send path and re-check the tier rule."
    )


def test_the_tier_definitions_still_declare_clicked_so_the_gap_is_a_gap():
    """Guards the other direction: this whole file is pointless if someone quietly drops `clicked` from
    the tier signals. That would be a legitimate resolution — Fazal amending the ratified rule to two
    signals — but it is a PRODUCT DECISION, not a cleanup, and it must not happen silently. If the
    tiers stop claiming `clicked`, this test fails and asks for the decision to be recorded."""
    text = _FREQ.read_text()
    assert 'signals=("replied", "clicked")' in text and '"read", "clicked", "replied"' in text, (
        "the tier definitions no longer declare `clicked`. If Fazal amended the ratified rule to the "
        "two signals that exist, record that decision in the ledger and retire this test with it — "
        "do not let the ratified rule change by deletion."
    )


def test_clicked_is_never_a_PRECONDITION_so_the_rule_counts_it_the_moment_it_appears():
    """D-B part 2, asserted behaviourally rather than assumed.

    The ruling asks that `clicked` count WHENEVER OBSERVED but never gate a tier. That needs no code
    change today — `clicked` is one signal among several in a FIRST-MATCH test — but "needs no change"
    is exactly the kind of claim that quietly stops being true. So: a customer with ONLY a click, no
    reply and no read, must reach Tier A; and a customer with only a reply must still reach Tier A
    with the click absent.
    """
    from orchestrator.agents.send_frequency import _TIER_ORDER, EngagementSignals

    def _first_match(sig: EngagementSignals) -> str:
        return next(t.name for t in _TIER_ORDER if t.matches(sig))

    assert _first_match(EngagementSignals(clicked_age_days=3.0)) == "A", (
        "a click alone no longer reaches Tier A — the signal has become a second-class citizen, "
        "which is what D-B forbids"
    )
    assert _first_match(EngagementSignals(replied_age_days=3.0)) == "A", (
        "a reply alone no longer reaches Tier A — clicked has become a PRECONDITION, the exact "
        "inversion D-B rules out"
    )
    assert _first_match(EngagementSignals(clicked_age_days=60.0)) == "B", (
        "a click outside 30d but inside 90d must still count, at Tier B"
    )
    assert _first_match(EngagementSignals()) == "C", "no signal must still resolve to the safe rung"
