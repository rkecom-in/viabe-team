"""VT-583 (CL-2026-07-03-conversing-surfaces-and-harness + -fluid-consent-and-control) — the PURE
(no-DB, no-live-LLM) surface of the conversing conversions:

  - the deterministic consent-reply floor (classify_consent_intent) — ZERO LLM, conservative;
  - the paced-flow / shopify intent classifiers' fail-soft contract (injected llm_fn; off-label + error
    → None so the caller keeps today's deterministic behavior);
  - the CONVERSE_STATUS_QUERIES flag default + off switch;
  - the consent-ask SUBSTANCE-RAIL (the disclosure substance survives whatever phrasing wraps it).

These run with no key on the env, so the real LLM helpers degrade to None (the key guard) — the
deterministic floors + fail-soft contracts are what's asserted here.
"""

from __future__ import annotations

import pytest

# These modules are logic-pure (no DB/LLM at call time) but carry heavy TOP-LEVEL imports —
# pre_filter_gate imports dbos, shopify_onboarding imports psycopg — so the dep-less smoke skips this
# file (the same importorskip discipline the realdb suites use); the full DB CI job runs it.
pytest.importorskip("dbos")
pytest.importorskip("psycopg")


# --- C: deterministic consent-reply floor (ZERO LLM) ----------------------------------------------


@pytest.mark.parametrize(
    "body",
    ["yes", "Yes please", "haan", "ok start", "start", "sure", "chalo karo", "activate", "जी हाँ"],
)
def test_consent_affirm_floor(body):
    from orchestrator.pre_filter_gate import classify_consent_intent

    assert classify_consent_intent(body) == "affirm"


@pytest.mark.parametrize(
    "body", ["no", "later", "not now", "nahi", "abhi nahi", "no thanks", "बाद में", "skip"]
)
def test_consent_decline_floor(body):
    from orchestrator.pre_filter_gate import classify_consent_intent

    assert classify_consent_intent(body) == "decline"


@pytest.mark.parametrize(
    "body",
    [
        "what does enabling do?",          # a question is never a decision
        "yes but not now",                  # both signals → conservative None
        "hmm maybe",                        # neither
        "tell me more about this",          # unrelated
        "",                                 # empty
    ],
)
def test_consent_ambiguous_is_none_never_guesses(body):
    """A consent GRANT must never ride on a guess — ambiguous / question / both-signals → None (re-ask)."""
    from orchestrator.pre_filter_gate import classify_consent_intent

    assert classify_consent_intent(body) is None


# --- C: SUBSTANCE-RAIL — the disclosure substance is present in the consent ask --------------------


def test_consent_prompt_pins_disclosure_substance():
    """The consent ask must carry WHAT enabling means (data processing) + HOW to pause (STOP) + the
    exact enable phrase — the substance is railed even though the phrasing is free (CL-2026-07-03)."""
    from orchestrator.direct_handlers.consent_required_handler import _CONSENT_PROMPT, _ENABLE_PHRASE

    low = _CONSENT_PROMPT.lower()
    assert "process your messages" in low or "read your" in low  # what enabling means
    assert "customer data" in low                                 # the data class disclosed
    assert "recover sales" in low                                 # the purpose disclosed
    assert "stop" in low                                          # how to pause
    assert _ENABLE_PHRASE in _CONSENT_PROMPT                       # the exact grant phrase


# --- A: paced-flow intent classifier — fail-soft contract -----------------------------------------


@pytest.mark.parametrize("intent", ["affirm", "decline", "connect", "other"])
def test_flow_intent_passes_valid_labels(intent):
    from orchestrator.onboarding.turn_brain import classify_flow_intent

    assert classify_flow_intent("whatever", llm_fn=lambda _b: intent) == intent


def test_flow_intent_off_label_and_error_and_none_all_map_to_none():
    """Off-label, a raising classifier, and a None result ALL collapse to None so the journey keeps its
    exact pre-VT-583 deterministic behavior (fail-soft = today's behavior)."""
    from orchestrator.onboarding.turn_brain import classify_flow_intent

    assert classify_flow_intent("x", llm_fn=lambda _b: "banana") is None
    assert classify_flow_intent("x", llm_fn=lambda _b: None) is None

    def _boom(_b):
        raise RuntimeError("llm down")

    assert classify_flow_intent("x", llm_fn=_boom) is None


def test_flow_intent_real_path_no_key_is_none(monkeypatch):
    """With no usable Anthropic key on the env, the real classifier makes NO live call and returns None."""
    from orchestrator.onboarding.turn_brain import classify_flow_intent

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # No llm_fn → real _llm_classify_flow_intent → key guard → None (no live call).
    assert classify_flow_intent("sure lets go") is None


# --- B: shopify auth intent classifier — fail-soft contract ---------------------------------------


@pytest.mark.parametrize("intent", ["done", "link", "other"])
def test_auth_intent_passes_valid_labels(intent):
    from orchestrator.onboarding.shopify_onboarding import classify_auth_intent

    assert classify_auth_intent("whatever", llm_fn=lambda _b: intent) == intent


def test_auth_intent_off_label_and_error_map_to_none():
    from orchestrator.onboarding.shopify_onboarding import classify_auth_intent

    assert classify_auth_intent("x", llm_fn=lambda _b: "nonsense") is None

    def _boom(_b):
        raise RuntimeError("down")

    assert classify_auth_intent("x", llm_fn=_boom) is None


def test_auth_intent_real_path_no_key_is_none(monkeypatch):
    from orchestrator.onboarding.shopify_onboarding import classify_auth_intent

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert classify_auth_intent("all set up now") is None


def test_auth_waiting_line_always_nonempty_even_without_walkthrough():
    """VT-583 D3 — the honest waiting line ALWAYS returns something to send, incl. when there is NO
    walkthrough_url (the :405 silent edge). With no link it tells the owner how to get a fresh one."""
    from orchestrator.onboarding.shopify_onboarding import _auth_waiting_line

    with_link = _auth_waiting_line("https://x.myshopify.com/admin/oauth")
    without_link = _auth_waiting_line(None)
    assert with_link and "myshopify.com" in with_link
    assert without_link and "link" in without_link.lower()


# --- E: CONVERSE_STATUS_QUERIES flag --------------------------------------------------------------


def test_converse_status_queries_default_on(monkeypatch):
    from orchestrator import pre_filter_gate

    monkeypatch.delenv("CONVERSE_STATUS_QUERIES", raising=False)
    assert pre_filter_gate._converse_status_queries() is True


@pytest.mark.parametrize("off", ["0", "false", "no", "off", "FALSE"])
def test_converse_status_queries_off_switch(monkeypatch, off):
    from orchestrator import pre_filter_gate

    monkeypatch.setenv("CONVERSE_STATUS_QUERIES", off)
    assert pre_filter_gate._converse_status_queries() is False


@pytest.mark.parametrize("on", ["1", "true", "yes", "on"])
def test_converse_status_queries_on_values(monkeypatch, on):
    from orchestrator import pre_filter_gate

    monkeypatch.setenv("CONVERSE_STATUS_QUERIES", on)
    assert pre_filter_gate._converse_status_queries() is True
