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
    ],
)
def test_is_setup_status_ask(body, expected):
    assert eg._is_setup_status_ask(body) is expected


# --- routing ------------------------------------------------------------------------------


def test_question_falls_through_to_brain(spies):
    assert _run("Hold on — why do you even need all these details about my shop?") is None
    assert spies["walker"] == [] and spies["sends"] == []


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
    assert "What do you sell?" in sent, "the honest status carries the pending question"


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
