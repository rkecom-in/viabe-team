"""VT-384 (C-b) — the RULE-ORDER PIN for the pre_filter gate.

Cowork ruling 20260612T140000Z condition 4: a RULE-ORDER PIN TEST — the authoritative
opt-out / DSR handlers provably run BEFORE the L3 kill matcher (and before the L3-ENABLE
matcher), asserted STRUCTURALLY (the order SOURCE itself), so a future insertion can't
silently reorder the authoritative-first discipline. This is NOT just behavioral: a
behavioral test only proves the order for the inputs it tries; the structural assert pins
the order of the RULES THEMSELVES, catching a reorder even for an input no behavioral test
exercises.

Why this matters (CL-438 floor + the live DPDP bug VT-369 CRITICAL-1): the autonomy_offer
copy promises the owner "you can always say STOP to turn this off" — so the kill keyword IS
the standing opt-out class. opt-out / DSR routing must ALWAYS win: an owner who says "STOP"
or "delete my data" while an L3 hold is in flight must reach the opt-out / DSR handler,
never a kill-keyword or ENABLE branch that swallows it. The authoritative-first order is the
DPDP-safe direction; this test makes a silent reorder fail CI.

Two assertions, belt-and-braces:
  (1) STRUCTURAL — the order source. If B2 refactors pre_filter to a declarative rule list
      (the preferred shape), assert the opt-out + DSR rules' list indices precede the kill +
      ENABLE rules'. Otherwise fall back to the source-text positions of the matcher
      branches in ``pre_filter`` (inline-if shape). Either way the ORDER SOURCE is pinned.
  (2) BEHAVIORAL corroboration — a body that contains BOTH an opt-out keyword AND the L3
      kill/ENABLE keyword routes to opt_out_handler (the authoritative wins the tie).

B2 builds the kill + L3-ENABLE rules concurrently. Until they are in pre_filter the
structural assert SKIPS (the kill/ENABLE rule markers are absent) rather than failing — the
integrator re-runs once B2 is merged. The opt-out/DSR-before-existing-ENABLE order is pinned
unconditionally (those rules exist today).
"""

from __future__ import annotations

import inspect
import os
import re

import pytest

pytest.importorskip("dbos")
pytest.importorskip("langgraph")

import orchestrator.pre_filter_gate as gate_mod  # noqa: E402

# Source of the routing function — the ORDER SOURCE for the inline-if shape.
_SRC = inspect.getsource(gate_mod.pre_filter)


def _first_pos(*needles: str) -> int | None:
    """The earliest source position at which ANY of the needles appears (regex), or None
    if none appear. Used to compare the textual order of rule branches in pre_filter."""
    best: int | None = None
    for needle in needles:
        m = re.search(needle, _SRC)
        if m is not None and (best is None or m.start() < best):
            best = m.start()
    return best


# ---------------------------------------------------------------------------
# (1) STRUCTURAL — the order source itself
# ---------------------------------------------------------------------------


def test_optout_and_dsr_precede_existing_enable_rule_structurally():
    """Pinned UNCONDITIONALLY (these rules exist today): in the pre_filter SOURCE the
    opt-out matcher and the DSR matcher both appear. The opt-out matcher precedes the
    DSR matcher (DPDP: opt-out is the strongest authoritative signal), and BOTH precede
    the substantive brain fall-through. This pins the authoritative-first skeleton that
    the kill/ENABLE insertion must slot AFTER."""
    optout = _first_pos(r"_OPT_OUT_PATTERNS\b", r"opt_out_handler")
    dsr = _first_pos(r"_DSR_PATTERNS\b", r"dsr_handler")
    brain = _first_pos(r"substantive owner message", r"RouteToBrain\(reason=\"unknown")
    assert optout is not None, "the opt-out matcher must exist in pre_filter"
    assert dsr is not None, "the DSR matcher must exist in pre_filter"
    assert brain is not None, "the brain fall-through must exist in pre_filter"
    assert optout < dsr, "opt-out must be matched BEFORE DSR (authoritative-first)"
    assert optout < brain and dsr < brain, (
        "opt-out + DSR must both precede the brain fall-through"
    )


def test_kill_matcher_follows_optout_and_dsr_structurally():
    """C-b CORE — the kill matcher's ORDER SOURCE. B2 adds an L3 kill-keyword rule to
    pre_filter; this asserts STRUCTURALLY that it appears AFTER both the opt-out and DSR
    matchers in the rule source. If B2 has not landed the kill rule yet (no kill marker in
    the source), SKIP — the integrator re-runs once B2 is merged. A reorder that moves the
    kill matcher above opt-out/DSR fails THIS test, even with no behavioral input
    exercising it."""
    kill = _first_pos(
        r"kill[_ ]?keyword", r"autonomy_kill", r"l3_kill", r"_KILL_PATTERNS",
        r"kill_handler", r"autonomy_kill_handler",
    )
    if kill is None:
        pytest.skip("B2 kill-keyword rule not yet in pre_filter — integrator re-runs")
    optout = _first_pos(r"_OPT_OUT_PATTERNS\b", r"opt_out_handler")
    dsr = _first_pos(r"_DSR_PATTERNS\b", r"dsr_handler")
    assert optout is not None and dsr is not None
    assert optout < kill, (
        "RULE-ORDER PIN: the opt-out matcher MUST precede the kill matcher in the source "
        "(authoritative-first; a reorder would let a kill branch swallow an opt-out)"
    )
    assert dsr < kill, (
        "RULE-ORDER PIN: the DSR matcher MUST precede the kill matcher in the source"
    )


def test_l3_enable_matcher_follows_optout_and_dsr_structurally():
    """C-b companion: the L3-ENABLE rule (B2) must ALSO slot after opt-out/DSR — an owner
    who types 'STOP enable' must opt out, not enable. SKIPs until B2's L3-ENABLE rule lands.
    (Distinct from the pre-existing data_inputs ENABLE rule — this is the L3 autonomy ENABLE
    keyword set; matched by its own marker.)"""
    l3_enable = _first_pos(
        r"l3[_ ]?enable", r"autonomy_enable", r"_L3_ENABLE", r"autonomy_enable_handler",
    )
    if l3_enable is None:
        pytest.skip("B2 L3-ENABLE rule not yet in pre_filter — integrator re-runs")
    optout = _first_pos(r"_OPT_OUT_PATTERNS\b", r"opt_out_handler")
    dsr = _first_pos(r"_DSR_PATTERNS\b", r"dsr_handler")
    assert optout is not None and dsr is not None
    assert optout < l3_enable and dsr < l3_enable, (
        "the L3-ENABLE matcher MUST follow opt-out + DSR (an opt-out inside an enable body wins)"
    )


def test_declarative_rule_list_order_if_present():
    """If B2 refactors pre_filter to a DECLARATIVE rule list (a module-level ordered tuple
    of (matcher, handler) — the preferred shape that makes order data, not control flow),
    pin the order by LIST INDEX: opt-out + DSR entries precede kill + ENABLE entries. This
    is the strongest form of the order-source pin (it reads the data, not the source text).
    SKIPs when no such list exists (the inline-if shape — covered by the source-text asserts
    above)."""
    rule_list = None
    for name in ("_RULES", "RULES", "_PRE_FILTER_RULES", "_RULE_ORDER", "_ROUTING_RULES"):
        if hasattr(gate_mod, name):
            rule_list = getattr(gate_mod, name)
            break
    if rule_list is None:
        pytest.skip("pre_filter uses inline-if rules (no declarative list) — source asserts cover it")

    # Render each rule entry to a comparable string (handler name / matcher repr).
    rendered = [repr(r).lower() for r in rule_list]

    def _idx(*needles: str) -> int | None:
        for i, r in enumerate(rendered):
            if any(n in r for n in needles):
                return i
        return None

    optout_i = _idx("opt_out", "optout")
    dsr_i = _idx("dsr")
    kill_i = _idx("kill")
    enable_i = _idx("l3_enable", "autonomy_enable")
    assert optout_i is not None and dsr_i is not None, "opt-out + DSR rules must be in the list"
    for label, j in (("kill", kill_i), ("L3-ENABLE", enable_i)):
        if j is None:
            continue  # that B2 rule not in the list yet — integrator re-runs
        assert optout_i < j and dsr_i < j, (
            f"declarative rule list: opt-out + DSR must precede the {label} rule by index"
        )


# ---------------------------------------------------------------------------
# (2) BEHAVIORAL corroboration — the authoritative wins a mixed body
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — pre_filter behavioral leg skipped",
)


@pytest.fixture(scope="module")
def gate():
    """Migrations + DBOS + the gate (mirrors test_pre_filter.py's fixture)."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from types import SimpleNamespace

    from dbos_config import launch_dbos, shutdown_dbos
    from orchestrator import types
    from orchestrator.pre_filter_gate import pre_filter
    from orchestrator.state import new_subscriber_state

    launch_dbos()
    try:
        yield SimpleNamespace(
            dsn=dsn, pre_filter=pre_filter, t=types, make_state=new_subscriber_state
        )
    finally:
        shutdown_dbos()


def _inbound(gate, body: str):
    return gate.t.WebhookEvent(body=body, sender_phone="+910000000000")


def test_mixed_optout_and_kill_body_routes_to_opt_out(gate):
    """Behavioral corroboration of the structural pin: a body that CONTAINS an opt-out
    keyword routes to opt_out_handler even if it would also match a future kill/ENABLE
    keyword — the authoritative-first order wins the tie. A pure 'STOP' is the kill keyword
    per the CL-438 copy AND the opt-out keyword; it must route opt_out (the strongest
    authoritative signal), never a kill branch that drops the DPDP guarantee."""
    from uuid import uuid4

    sub = gate.make_state(uuid4())
    for body in ("STOP", "please STOP enable", "बंद करो"):
        result = gate.pre_filter(_inbound(gate, body), sub)
        assert isinstance(result, gate.t.RouteToDirectHandler), f"{body!r} → {result!r}"
        assert result.handler_name == "opt_out_handler", (
            f"{body!r} must route opt_out_handler (authoritative-first), got "
            f"{result.handler_name!r}"
        )
