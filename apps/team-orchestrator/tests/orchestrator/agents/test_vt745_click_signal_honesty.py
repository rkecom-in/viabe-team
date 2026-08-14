"""VT-745 — the click signal is unobtainable, and this test keeps that statement honest.

WHY THIS FILE EXISTS INSTEAD OF A FIX. VT-745 was rostered as "mint the customer hook link wherever a
customer-facing link goes out". Investigation 2026-08-14 found that premise is false in two independent
ways:

  1. No customer-audience template declares a URL/link variable, so there is nowhere in an approved
     customer message to put a tracked link. (`trial_subscribe_link`, the only link-bearing template,
     is audience=owner.) Free-form is not an escape — it needs an open 24h window and a win-back
     targets lapsed customers by definition.
  2. `GET /r/{token}` redirects to the tenant's own `wa.me`. It is an inbound-acquisition primitive for
     surfaces OUTSIDE WhatsApp; sending it to a customer already in WhatsApp redirects them to where
     they already are.

So `clicked` is not un-wired, it is unobtainable through any channel this product has, and closing it
is a Fazal/Meta decision (a new customer template with a URL variable, or amending the ratified tier
rule to the two signals that exist). The honest deliverable is therefore the shortfall note at the point
of the read — plus this test, so the note cannot rot.

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
    for evidence in ("audience=owner", "wa.me", "unobtainable"):
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
