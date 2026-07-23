"""T14 — the enforce-mode narrow journey gate: speech-act routing truth table.

Pure tests (no DB/Twilio): ``get_journey`` / ``maybe_handle_journey_reply`` / the send seam are
monkeypatched. The routing contract under test (module docstring rules A-D):
questions → None (brain); setup-status asks → deterministic honest answer; kickoff button +
non-interrogative in-flight turns → the walker; post-completion chatter → None (never the
post-profile flow's scripted pitch — the measured Shopify-assumption fabrication).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.onboarding import enforce_journey_gate as eg  # noqa: E402

_TID = str(uuid4())
_RECIP = "+919811110000"


def _active_journey(remaining_prompt: str = "What do you sell?") -> dict:
    return {
        "status": "active",
        "question_queue": [{"field": "about", "prompt_en": remaining_prompt, "prompt_hi": ""}],
        "cursor": 0,
        "answers": {},
        "skipped": [],
    }


@pytest.fixture
def spies(monkeypatch):
    calls = {"walker": [], "sends": [], "journey": _active_journey()}
    monkeypatch.setattr(
        "orchestrator.onboarding.journey.get_journey", lambda tenant_id: calls["journey"]
    )
    monkeypatch.setattr(
        "orchestrator.onboarding.journey.maybe_handle_journey_reply",
        lambda tenant_id, body, sid, recipient: calls["walker"].append(body) or {"done": False},
    )

    # R9 item 3 — the DF4 connect-offer marker writes to onboarding_journey (no DB here). Stub it to
    # persist the marker IN the mock journey's answers so a two-turn disambiguation test is realistic
    # (and existing single-turn menu tests never touch the DB).
    def _mark_offer(tenant_id, message_sid=None):
        j = calls["journey"]
        if isinstance(j, dict):
            j.setdefault("answers", {})["__connect_offer_at__"] = "true"

    monkeypatch.setattr(
        "orchestrator.onboarding.journey._set_connect_offer_marker", _mark_offer
    )
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale", lambda tenant_id: "en"
    )
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack",
        lambda tenant_id, recipient, body: calls["sends"].append(body) or True,
    )
    return calls


def _run(body: str):
    return eg.maybe_handle_enforce_journey_turn(_TID, body, "SM" + "0" * 32, _RECIP)


# --- classifier truth table ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Hold on — why do you even need all these details about my shop?", True),
        ("So are we set up now?", True),
        ("kya sab ho gaya", True),
        ("Ok that's fair enough. It's Sharma Hardware, we sell tools, in Karol Bagh.", False),
        ("Complete Setup", False),
        ("hello there", False),
        # DF7(c) — a "how long"-lead Hinglish duration ask routes to the brain (no '?' needed).
        ("kitna time lagega setup complete karne mein", True),
    ],
)
def test_is_interrogative(body, expected):
    assert eg._is_interrogative(body) is expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("So are we set up now?", True),
        ("is the setup done?", True),
        ("setup ho gaya?", True),
        ("why do you need all these details about my shop?", False),
        ("It's Sharma Hardware, we sell tools", False),
        # DF7(c) — a DURATION ask carries a setup token + "complete" status cue, yet is NOT a status ask.
        ("kitna time lagega setup complete karne mein", False),
        ("how long will the setup take?", False),
    ],
)
def test_is_setup_status_ask(body, expected):
    assert eg._is_setup_status_ask(body) is expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("what else do you need from me", True),
        ("aur kuch chahiye?", True),
        ("kuch aur bataana hai?", True),
        ("why do you need all this?", False),
        ("It's Sharma Hardware", False),
    ],
)
def test_is_remaining_needs_ask(body, expected):
    assert eg._is_remaining_needs_ask(body) is expected


# --- routing ------------------------------------------------------------------------------


def test_question_falls_through_to_brain(spies, monkeypatch):
    # ACTIVATED tenant (owner_inputs true): the brain owns questions — the measured-good path.
    monkeypatch.setattr(
        "orchestrator.memory.l0_writer._owner_inputs_enabled", lambda t: True
    )
    assert _run("Hold on — why do you even need all these details about my shop?") is None
    assert spies["walker"] == [] and spies["sends"] == []


def test_preactivation_question_delegates_to_journey_brain(spies, monkeypatch):
    """VT-703 (sim-caught): pre-activation there IS no brain downstream — the question must go
    to the journey turn-brain (never-deflect), not dead-end into the consent represent guard."""
    monkeypatch.setattr(
        "orchestrator.memory.l0_writer._owner_inputs_enabled", lambda t: False
    )
    res = _run("What does that mean?")
    assert res is not None and spies["walker"] == ["What does that mean?"]


def test_preactivation_read_failure_keeps_brain_routing(spies, monkeypatch):
    def _boom(t):
        raise RuntimeError("db down")

    monkeypatch.setattr("orchestrator.memory.l0_writer._owner_inputs_enabled", _boom)
    assert _run("why do you need my GST number?") is None
    assert spies["walker"] == []


def test_volunteered_details_route_to_walker(spies):
    res = _run("Ok that's fair enough. It's Sharma Hardware, we sell tools, in Karol Bagh.")
    assert res is not None
    assert len(spies["walker"]) == 1


def test_kickoff_button_routes_to_walker(spies):
    res = _run("Complete Setup")
    assert res is not None
    assert len(spies["walker"]) == 1


def test_setup_status_ask_answered_deterministically_active(spies):
    res = _run("So are we set up now?")
    assert res is not None and res["routed_kind"] == "journey_status"
    assert spies["walker"] == [], "the status ask must not hit the scripted walker"
    assert len(spies["sends"]) == 1
    sent = spies["sends"][0]
    assert "Not quite yet" in sent
    # R9 item 2 — the status NAMES the need with a short label; it does NOT re-paste the verbatim
    # prompt sentence (a verbatim repeat reads as a loop_stall), and pluralizes 'detail'.
    assert "what you sell or do" in sent.lower(), "the status names the pending need with a short label"
    assert "What do you sell?" not in sent, "must NOT re-paste the verbatim prompt sentence"
    assert "1 quick detail to go" in sent, "one remaining → singular 'detail'"


def test_setup_status_ask_pluralizes_multiple_remaining(spies):
    # R9 item 2 — two remaining details → plural 'details', still a short need label (the cursor head).
    spies["journey"] = {
        "status": "active",
        "question_queue": [
            {"field": "about", "prompt_en": "What do you sell?", "prompt_hi": ""},
            {"field": "city", "prompt_en": "Which city?", "prompt_hi": ""},
        ],
        "cursor": 0,
        "answers": {},
        "skipped": [],
    }
    res = _run("are we set up now?")
    assert res is not None and res["routed_kind"] == "journey_status"
    sent = spies["sends"][0]
    assert "2 quick details to go" in sent, "two remaining → plural 'details'"
    assert "what you sell or do" in sent.lower()


def test_setup_status_ask_completed_journey_states_fact_no_pitch(spies):
    spies["journey"] = {"status": "completed", "question_queue": [], "cursor": 0}
    res = _run("are we set up now?")
    assert res is not None and res["done"] is True
    sent = spies["sends"][0]
    assert "your business profile is set up" in sent
    assert "Shopify" not in sent, "never assume/pitch a platform (the measured fabrication)"


def test_post_completion_chatter_falls_through_to_brain(spies):
    spies["journey"] = {"status": "completed", "question_queue": [], "cursor": 0}
    assert _run("thanks, sounds good") is None
    assert spies["walker"] == []


def test_no_journey_row_and_not_kickoff_falls_through(spies):
    spies["journey"] = None
    assert _run("hello there") is None
    assert spies["walker"] == []


def test_opt_out_never_consumed_by_status_answer(spies, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.pre_filter_gate.matches_opt_out_or_dsr", lambda body: True
    )
    assert _run("STOP — are we set up now?") is None
    assert spies["sends"] == []


def test_gate_fails_open_on_error(spies, monkeypatch):
    def _boom(tenant_id):
        raise RuntimeError("db down")

    monkeypatch.setattr("orchestrator.onboarding.journey.get_journey", _boom)
    assert _run("Ok it's Sharma Hardware") is None


# --- DF7(b) — confirm-correction routing (active journey, confirm head) --------------------


def _active_confirm_journey() -> dict:
    return {
        "status": "active",
        "question_queue": [{
            "field": "business_type", "kind": "confirm",
            "prompt_en": "We found you're a footwear business — is that right?",
            "prompt_hi": "", "draft_value": "footwear",
        }],
        "cursor": 0,
        "answers": {},
        "skipped": [],
    }


def test_confirm_contradiction_routes_to_walker(spies):
    # R9 item 4 — a rich, NON-bare correction on a confirm head now DELEGATES to the walker, whose
    # own DF7(b) branch (journey.handle_reply → _reprompt_after_no) re-prompts it deterministically
    # (measured 3/3 vs the brain's 2/3 wrong re-assertion). It is no longer forked to the brain.
    spies["journey"] = _active_confirm_journey()
    res = _run("nahi bhai hum footwear nahi bechte, hum leather bags bechte hain")
    assert res is not None
    assert spies["walker"] == ["nahi bhai hum footwear nahi bechte, hum leather bags bechte hain"], (
        "the rich confirm-contradiction now delegates to the walker (handle_reply owns DF7(b))"
    )


def test_bare_no_on_confirm_still_goes_to_walker(spies):
    # A BARE 'no' is EXCLUDED from the brain-route — it keeps the walker's good _reprompt_after_no.
    spies["journey"] = _active_confirm_journey()
    res = _run("nahi")
    assert res is not None
    assert spies["walker"] == ["nahi"], "bare 'no' must delegate to the walker"


def test_active_duration_ask_routes_to_brain(spies):
    # DF7(c) — an ACTIVE journey + a DURATION ask is not a status ask; it routes to the brain (rule D),
    # never the canned status line, never the walker.
    spies["journey"] = _active_confirm_journey()
    assert _run("kitna time lagega setup complete karne mein") is None
    assert spies["walker"] == [] and spies["sends"] == []


def test_remaining_needs_answered_from_row_before_brain(spies):
    # DF7(d) — "what else do you need" is answered from the row (remaining count + pending prompt),
    # BEFORE the interrogative fall-through. Never the walker.
    spies["journey"] = _active_journey("What do you sell?")
    res = _run("what else do you need from me")
    assert res is not None and res["routed_kind"] == "journey_remaining_needs"
    assert res["done"] is False
    assert spies["walker"] == []
    assert len(spies["sends"]) == 1
    sent = spies["sends"][0]
    # R9 item 2 — names the need with a short label, never the verbatim prompt sentence.
    assert "Not quite yet" in sent and "what you sell or do" in sent.lower()
    assert "What do you sell?" not in sent


# --- DF4 — post-profile connect beat (completed journey, paced flow) -----------------------


def _completed_flow_journey(flow: str) -> dict:
    return {
        "status": "complete",
        "question_queue": [],
        "cursor": 0,
        "answers": {"__flow__": flow},
        "skipped": [],
    }


@pytest.fixture
def _no_live_resume(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.onboarding.shopify_onboarding.has_live_resume", lambda tid: False
    )


def test_ready_asked_affirm_offers_connect_menu(spies, monkeypatch, _no_live_resume):
    # No store domain in-window → an HONEST two-option MENU (Shopify OR Google Sheet), never a
    # single-pick Shopify pitch (CD1). The menu also names the *.myshopify.com shape.
    spies["journey"] = _completed_flow_journey("ready_asked")
    monkeypatch.setattr(
        "orchestrator.onboarding.journey._recent_shop_domain",
        lambda tid, current_body=None: None,
    )
    res = _run("Yes let's connect")
    assert res is not None and res["routed_kind"] == "journey_connect_offer"
    assert res["done"] is False
    assert spies["walker"] == []
    sent = spies["sends"][0]
    assert "Shopify" in sent and "Google Sheet" in sent, "must be a MENU, both options named"
    assert "myshopify.com" in sent


def test_ready_asked_second_connect_intent_disambiguates_not_repeat(spies, monkeypatch, _no_live_resume):
    # R9 item 3 — a FIRST connect-intent with no store domain → the two-option menu (marker set). A
    # SECOND connect-intent with STILL no domain → a short non-byte-equal disambiguation, NOT the
    # identical menu (a verbatim repeat reads as a loop_stall). The spies fixture persists the marker.
    spies["journey"] = _completed_flow_journey("ready_asked")
    monkeypatch.setattr(
        "orchestrator.onboarding.journey._recent_shop_domain",
        lambda tid, current_body=None: None,
    )
    res1 = _run("Yes let's connect")
    assert res1 is not None and res1["routed_kind"] == "journey_connect_offer"
    menu = spies["sends"][-1]
    assert "Shopify" in menu and "Google Sheet" in menu, "first turn is the full menu"

    res2 = _run("come on, connect it na")
    assert res2 is not None and res2["routed_kind"] == "journey_connect_offer"
    disamb = spies["sends"][-1]
    assert disamb != menu, "the second connect-intent must NOT re-send the byte-identical menu"
    assert "which do you have" in disamb.lower(), "the second turn is the short disambiguation"


def test_ready_asked_affirm_with_domain_offers_onetap_link(spies, monkeypatch, _no_live_resume):
    # A store domain already captured in-window → surface the one-tap OAuth link (honest: the owner
    # chose Shopify by naming their store).
    spies["journey"] = _completed_flow_journey("ready_asked")
    monkeypatch.setattr(
        "orchestrator.onboarding.journey._recent_shop_domain",
        lambda tid, current_body=None: "sundaram-sweets.myshopify.com",
    )
    minted = {}
    def _fake_setup(tid, shop, **kw):
        minted["shop"] = shop
        return {"authorize_url": "https://viabe.example/oauth?state=xyz"}
    monkeypatch.setattr(
        "orchestrator.onboarding.shopify_onboarding.start_shopify_setup", _fake_setup
    )
    res = _run("Sure, my store address is sundaram-sweets.myshopify.com")
    assert res is not None and res["routed_kind"] == "journey_connect_offer"
    assert minted["shop"] == "sundaram-sweets.myshopify.com"
    sent = spies["sends"][0]
    assert "sundaram-sweets.myshopify.com" in sent
    assert "https://viabe.example/oauth?state=xyz" in sent


def test_ready_asked_interrogative_falls_to_brain(spies, monkeypatch, _no_live_resume):
    # A QUESTION on the ready_asked beat is never consumed as a connect signal (the affirm floor
    # excludes it AND rule D catches it first) → None → brain.
    spies["journey"] = _completed_flow_journey("ready_asked")
    assert _run("why do you need my data?") is None
    assert spies["sends"] == [] and spies["walker"] == []


def test_ready_asked_optout_short_circuits(spies, monkeypatch, _no_live_resume):
    # Opt-out ALWAYS wins over a connect signal → None (falls to pre_filter), never sends.
    spies["journey"] = _completed_flow_journey("ready_asked")
    monkeypatch.setattr(
        "orchestrator.pre_filter_gate.matches_opt_out_or_dsr", lambda body: True
    )
    assert _run("yes connect but STOP everything") is None
    assert spies["sends"] == []


def test_connect_defers_when_integration_resume_live(spies, monkeypatch):
    # An integration handoff already in flight → DEFER to the downstream connector resume gate (None),
    # so a later 'done' hits the DB re-check there, not a second link mint here.
    spies["journey"] = _completed_flow_journey("ready_asked")
    monkeypatch.setattr(
        "orchestrator.onboarding.shopify_onboarding.has_live_resume", lambda tid: True
    )
    assert _run("done") is None
    assert spies["sends"] == []


def test_deferred_connect_intent_offers_menu(spies, monkeypatch, _no_live_resume):
    # A clear connect-intent on a DEFERRED flow re-engages (floor hit — no LLM) with the honest menu.
    spies["journey"] = _completed_flow_journey("deferred")
    monkeypatch.setattr(
        "orchestrator.onboarding.journey._recent_shop_domain",
        lambda tid, current_body=None: None,
    )
    res = _run("haan chalo ab shuru karte hain, data connect karte hain")
    assert res is not None and res["routed_kind"] == "journey_connect_offer"
    assert "Shopify" in spies["sends"][0] and "Google Sheet" in spies["sends"][0]


def test_deferred_chatter_falls_to_brain(spies, monkeypatch, _no_live_resume):
    # Non-connect chatter on a DEFERRED flow → None (brain gives the understanding ack). The floor
    # misses, so the classifier is consulted — mocked to 'other' (no network).
    spies["journey"] = _completed_flow_journey("deferred")
    monkeypatch.setattr(
        "orchestrator.onboarding.turn_brain.classify_flow_intent", lambda body: "other"
    )
    assert _run("thanks for your patience") is None
    assert spies["sends"] == []


def test_completed_non_flow_chatter_falls_to_brain(spies, monkeypatch, _no_live_resume):
    # A completed journey whose flow has finished (plan_kicked) → the connect beat does not own it → brain.
    spies["journey"] = _completed_flow_journey("plan_kicked")
    assert _run("Yes let's connect") is None
    assert spies["sends"] == []
