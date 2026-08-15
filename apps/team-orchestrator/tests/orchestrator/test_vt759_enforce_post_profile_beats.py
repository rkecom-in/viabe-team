"""VT-759 — the paced post-profile beats enforce mode never had.

WHAT THE ROW ORIGINALLY SAID, AND WHY IT WAS WRONG. VT-759 was rostered as an ASSERT defect: three
gate (d) failures where an English literal (`hold off`, `one at a time`, `sure`) was asserted against
a Hinglish reply, the behaviour having held. Re-driven on deployed dev 2026-08-15, two of the three
are not that at all:

    readiness_ask_then_defer_and_resume  step 0  route=none  surface=manager
    profile_preview_then_confirm         step 0  route=none  surface=manager

`route: none` + `surface: manager` means **no journey beat ran**. The asserts name deterministic
templates (`_DEFER_MSG`, `_READINESS_ASK`) that live in `journey._maybe_handle_post_profile_flow` —
and dev runs `TEAM_MANAGER_LOOP_MODE=enforce`, whose gate is `maybe_handle_enforce_journey_turn`,
which deliberately does NOT delegate that machine (its single-pick Shopify pitch is the measured
fabrication). DF4 ported ONE beat: the AFFIRM at `ready_asked`. The DECLINE and the profile-card
ACKNOWLEDGEMENT were never ported, so both fell to the brain.

So the asserts were RIGHT and the product was missing the beat. Changing the asserts to match the
brain's improvisation would have been exactly the "loosen an assert to make a run pass" the row's own
Boundaries forbid — and would have buried a real gap under a vocabulary story.

WHAT THE BRAIN DID INSTEAD, measured:

    decline  → "Theek hai, abhi pause kar dete hain. Jab aap ready hon, yahin se continue kar lenge."
               Semantically close, but composed, not the honest template — and `__flow__` was never
               set to `deferred`, so the flow was NOT resumable. The decline was answered and forgotten.

    card ack → "Is approval ka exact plan ya option mujhe yahan dikh nahi raha; uska naam ya ek line
               dobara bhej dijiye" — an APPROVAL context that does not exist, and a re-ask for
               something the owner never sent, on the turn right after they said yes.

The second is a fabrication on the honesty floor, not a phrasing miss.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.onboarding import enforce_journey_gate as gate  # noqa: E402
from orchestrator.onboarding import journey  # noqa: E402

_TENANT = str(uuid4())
_PHONE = "+919321553267"  # Fazal's number — the ONLY real number allowed; nothing is sent here.


def _journey(flow: str) -> dict:
    return {"status": "complete", "answers": {journey._FLOW_KEY: flow}}


@pytest.fixture
def calls(monkeypatch):
    """Record which beat fired instead of running it (no transport, no DB)."""
    seen: list[tuple[str, str]] = []

    def _defer(tenant_id, recipient, message_sid, lang):
        seen.append(("defer", lang))
        return {"done": True, "routed": "flow_deferred", "flow": journey._FLOW_DEFERRED}

    def _ask(tenant_id, recipient, message_sid, lang):
        seen.append(("readiness_ask", lang))
        return {"done": False, "routed": "flow_readiness_ask", "flow": journey._FLOW_READY_ASKED}

    monkeypatch.setattr(journey, "_flow_defer", _defer)
    monkeypatch.setattr(journey, "_flow_ask_readiness", _ask)
    monkeypatch.setattr(
        gate, "_owner_lang", lambda _t: "hi"
    )  # the harness scenarios are Hinglish; prove the beat honours it
    return seen


# --- the DECLINE beat -------------------------------------------------------------------------


def test_the_measured_decline_now_fires_the_defer_beat(calls):
    """The exact gate (d) message. Before: no beat, brain improvised, flow left un-armed."""
    r = gate._maybe_post_profile_defer(
        _TENANT, "nahi abhi nahi, baad mein karenge", None, _PHONE, _journey(journey._FLOW_READY_ASKED)
    )
    assert calls == [("defer", "hi")], "the deterministic defer beat did not fire"
    assert r is not None and r["flow"] == journey._FLOW_DEFERRED, (
        "the beat must ARM THE RESUME — an answered decline that leaves __flow__ unchanged is the "
        "brain's behaviour, not the beat's"
    )


def test_defer_also_covers_the_intro_and_trial_beats(calls):
    """`later` means defer at every beat the legacy machine deferred from, not only ready_asked."""
    for flow in (journey._FLOW_INTRO_SENT, journey._FLOW_TRIAL_SENT):
        calls.clear()
        gate._maybe_post_profile_defer(_TENANT, "baad mein", None, _PHONE, _journey(flow))
        assert calls == [("defer", "hi")], f"no defer beat at flow={flow}"


def test_an_affirm_is_NOT_consumed_as_a_decline(calls):
    """DF4 owns the affirm — this beat must not race it."""
    assert (
        gate._maybe_post_profile_defer(
            _TENANT, "haan chalo karo", None, _PHONE, _journey(journey._FLOW_READY_ASKED)
        )
        is None
    )
    assert calls == []


def test_an_AMBIGUOUS_reply_goes_to_the_brain_not_the_beat(calls):
    """The deterministic floor only. `_resolve_readiness_intent` would map ambiguity to affirm and
    would put a classifier LLM inside a beat that exists to be deterministic."""
    assert (
        gate._maybe_post_profile_defer(
            _TENANT, "hmm dekhta hoon", None, _PHONE, _journey(journey._FLOW_READY_ASKED)
        )
        is None
    )
    assert calls == []


def test_a_decline_shaped_OPT_OUT_is_never_consumed_as_a_later(calls, monkeypatch):
    """DPDP floor: a STOP must reach the authoritative opt-out handler, never a friendly 'no problem,
    I'll hold off'. Nothing about this beat may soften an opt-out."""
    monkeypatch.setattr(gate, "_maybe_post_profile_connect", lambda *a, **k: None, raising=False)
    from orchestrator import pre_filter_gate

    real = pre_filter_gate.matches_opt_out_or_dsr
    assert real("stop"), "precondition: the matcher recognises this as an opt-out"
    assert (
        gate._maybe_post_profile_defer(
            _TENANT, "stop", None, _PHONE, _journey(journey._FLOW_READY_ASKED)
        )
        is None
    )
    assert calls == []


def test_no_recipient_falls_through_rather_than_going_silent(calls):
    assert (
        gate._maybe_post_profile_defer(
            _TENANT, "nahi abhi nahi", None, None, _journey(journey._FLOW_READY_ASKED)
        )
        is None
    )
    assert calls == []


# --- the PROFILE-CARD ACK beat ----------------------------------------------------------------


def test_the_measured_card_ack_now_advances_to_the_readiness_ask(calls):
    """The exact gate (d) message. Before: the brain answered it as a pending APPROVAL and asked the
    owner to re-send an option they never sent."""
    r = gate._maybe_post_profile_ack_advance(
        _TENANT, "haan bilkul, yehi sahi hai", None, _PHONE, _journey(journey._FLOW_PREVIEWED)
    )
    assert calls == [("readiness_ask", "hi")]
    assert r is not None and r["flow"] == journey._FLOW_READY_ASKED, (
        "the beat must advance the sentinel so DF4's connect beat owns the NEXT turn"
    )


def test_the_ack_beat_only_runs_at_previewed(calls):
    """It must not hijack ready_asked (DF4's) or deferred (resume)."""
    for flow in (journey._FLOW_READY_ASKED, journey._FLOW_DEFERRED, journey._FLOW_PLAN_KICKED):
        assert (
            gate._maybe_post_profile_ack_advance(_TENANT, "haan", None, _PHONE, _journey(flow))
            is None
        ), f"the ack beat wrongly claimed flow={flow}"
    assert calls == []


def test_a_decline_at_previewed_is_not_an_acknowledgement(calls):
    assert (
        gate._maybe_post_profile_ack_advance(
            _TENANT, "nahi, yeh galat hai", None, _PHONE, _journey(journey._FLOW_PREVIEWED)
        )
        is None
    )
    assert calls == []


# --- the gate wiring --------------------------------------------------------------------------


def test_both_beats_are_REACHABLE_from_the_enforce_gate(monkeypatch, calls):
    """A unit-green beat that the gate never calls is the VT-720 class of dead fix — assert the wiring,
    not just the function."""
    monkeypatch.setattr(journey, "get_journey", lambda _t: _journey(journey._FLOW_READY_ASKED))
    monkeypatch.setattr(gate, "_is_kickoff", lambda _t: False)
    monkeypatch.setattr(gate, "_is_setup_status_ask", lambda _t: False)
    monkeypatch.setattr(gate, "_is_interrogative", lambda _t: False)
    monkeypatch.setattr(gate, "_maybe_post_profile_connect", lambda *a, **k: None)

    r = gate.maybe_handle_enforce_journey_turn(_TENANT, "nahi abhi nahi, baad mein karenge", None, _PHONE)
    assert calls == [("defer", "hi")], "the gate did not reach the defer beat"
    assert r is not None and r["routed"] == "flow_deferred"

    calls.clear()
    monkeypatch.setattr(journey, "get_journey", lambda _t: _journey(journey._FLOW_PREVIEWED))
    r = gate.maybe_handle_enforce_journey_turn(_TENANT, "haan bilkul, yehi sahi hai", None, _PHONE)
    assert calls == [("readiness_ask", "hi")], "the gate did not reach the ack beat"
    assert r is not None and r["routed"] == "flow_readiness_ask"


def test_the_gate_still_returns_None_for_ordinary_chatter(monkeypatch, calls):
    """The narrowness is the point: everything these two beats do not own stays the brain's."""
    monkeypatch.setattr(journey, "get_journey", lambda _t: _journey(journey._FLOW_READY_ASKED))
    monkeypatch.setattr(gate, "_is_kickoff", lambda _t: False)
    monkeypatch.setattr(gate, "_is_setup_status_ask", lambda _t: False)
    monkeypatch.setattr(gate, "_is_interrogative", lambda _t: False)
    monkeypatch.setattr(gate, "_maybe_post_profile_connect", lambda *a, **k: None)

    assert gate.maybe_handle_enforce_journey_turn(_TENANT, "mera shop band tha kal", None, _PHONE) is None
    assert calls == []


def test_module_imports_the_names_it_uses():
    """Both beats import journey symbols lazily; a rename would surface only at runtime."""
    for name in (
        "_FLOW_PREVIEWED", "_FLOW_READY_ASKED", "_FLOW_DEFERRED", "_FLOW_INTRO_SENT",
        "_FLOW_TRIAL_SENT", "_FLOW_KEY", "_flow_defer", "_flow_ask_readiness",
        "_is_affirm", "_is_decline",
    ):
        assert hasattr(journey, name), f"journey.{name} is gone — the enforce beats import it"
