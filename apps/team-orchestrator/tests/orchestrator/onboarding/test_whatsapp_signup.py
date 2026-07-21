"""VT-691 — WhatsApp-initiated signup: consent gate + state machine (dep-less unit tests).

The DPDP asymmetry under test everywhere: a false 'unclear'/'declined' merely re-asks or goes
silent; a false 'consent' would create a tenant + record a consent proof never given. Every
uncertain/errored path MUST resolve away from consent.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

from orchestrator.onboarding import whatsapp_signup as ws  # noqa: E402

_PHONE = "+919900112233"


# --- classify_consent_reply ---------------------------------------------------------------------


def _llm_returning(payload: dict[str, Any]):
    def _call(tier, *, system, user, max_tokens, agent, call_site, tenant_id=None):
        return json.dumps(payload)

    return _call


def test_optout_dsr_veto_wins_over_everything(monkeypatch) -> None:
    """A STOP/DSR phrasing is a person telling us to go away — deterministic 'declined',
    LLM never consulted (veto runs first)."""
    def _explode(*a, **k):
        raise AssertionError("LLM must not run on a hard-stop")

    assert ws.classify_consent_reply("STOP", text_call=_explode) == "declined"
    assert ws.classify_consent_reply("band karo", text_call=_explode) == "declined"


def test_exact_prompt_instructed_tokens_consent_without_llm() -> None:
    def _explode(*a, **k):
        raise AssertionError("LLM must not run on an exact token")

    for reply in ("YES", "yes", " Haan ", "हाँ", "Agree", "I agree", "yes, I agree", "हाँ।"):
        assert ws.classify_consent_reply(reply, text_call=_explode) == "consent", reply


def test_free_text_containing_yes_is_not_exact() -> None:
    """'yes but first tell me the price' must NOT hit the exact fast-path — it goes to the
    LLM, and an unclear verdict re-asks."""
    called = {"n": 0}

    def _call(*a, **k):
        called["n"] += 1
        return json.dumps({"decision": "unclear", "cited_cue": "first tell me", "confidence": 0.9})

    out = ws.classify_consent_reply("yes but first tell me the price", text_call=_call)
    assert out == "unclear" and called["n"] == 1


def test_llm_consent_needs_grounding_and_confidence() -> None:
    body = "ha bilkul, sign me up please"
    ok = _llm_returning({"decision": "consent", "cited_cue": "sign me up", "confidence": 0.95})
    assert ws.classify_consent_reply(body, text_call=ok) == "consent"

    low_conf = _llm_returning({"decision": "consent", "cited_cue": "sign me up", "confidence": 0.5})
    assert ws.classify_consent_reply(body, text_call=low_conf) == "unclear"

    ungrounded = _llm_returning({"decision": "consent", "cited_cue": "yes I fully agree", "confidence": 0.99})
    assert ws.classify_consent_reply(body, text_call=ungrounded) == "unclear", \
        "a cited_cue that is not a verbatim substring must never ground a consent"


def test_llm_error_or_garbage_never_consents() -> None:
    def _boom(*a, **k):
        raise RuntimeError("provider down")

    assert ws.classify_consent_reply("thik hai chalo", text_call=_boom) == "unclear"

    def _garbage(*a, **k):
        return "I think they probably agree!"

    assert ws.classify_consent_reply("thik hai chalo", text_call=_garbage) == "unclear"


# --- handle_unknown_inbound state machine -------------------------------------------------------


def _wire(monkeypatch, *, session, classify=None):
    """Patch every side-effect seam; return the capture dict."""
    calls: dict[str, Any] = {
        "sent": [], "prompted": 0, "status": None, "consented": None, "created": 0,
        "journey": None,
    }
    monkeypatch.setattr(ws, "purge_stale", lambda **k: 0)
    monkeypatch.setattr(ws, "get_session", lambda p: session)
    monkeypatch.setattr(ws, "upsert_prompted", lambda p: calls.__setitem__("prompted", calls["prompted"] + 1))
    monkeypatch.setattr(ws, "mark_status", lambda p, s: calls.__setitem__("status", s))
    monkeypatch.setattr(ws, "mark_consented", lambda p, t: calls.__setitem__("consented", str(t)))
    monkeypatch.setattr(ws, "_send", lambda p, text: calls["sent"].append(text))
    if classify is not None:
        monkeypatch.setattr(ws, "classify_consent_reply", classify)

    tenant_id = uuid4()

    class _Res:
        pass

    res = _Res()
    res.tenant_id = tenant_id
    res.created = True

    import orchestrator.onboarding.signup as signup_mod

    def _create(phone, **k):
        calls["created"] += 1
        return res

    monkeypatch.setattr(signup_mod, "create_whatsapp_signup_tenant", _create, raising=False)

    import orchestrator.onboarding.journey as journey_mod

    def _kick(tid, body, sid, recipient, **k):
        calls["journey"] = {"tenant": str(tid), "body": body}
        return {"handled": True}

    monkeypatch.setattr(journey_mod, "maybe_handle_journey_reply", _kick)

    def _start(tid, queue):
        calls["journey_started"] = {"tenant": str(tid), "queue_fields": [q["field"] for q in queue]}

    monkeypatch.setattr(journey_mod, "start_journey", _start)
    monkeypatch.setattr(journey_mod, "get_journey", lambda tid: None)
    calls["tenant_id"] = str(tenant_id)
    return calls


def _pending(prompts=1, last_prompt_age_hours=24.0):
    return {
        "id": "s1", "status": "consent_pending", "consent_prompt_count": prompts,
        "last_prompt_at": datetime.now(timezone.utc) - timedelta(hours=last_prompt_age_hours),
        "tenant_id": None,
    }


def test_first_contact_prompts_consent(monkeypatch) -> None:
    calls = _wire(monkeypatch, session=None)
    out = ws.handle_unknown_inbound(_PHONE, "Hi", "SM1")
    assert out["outcome"] == "consent_prompted"
    assert calls["prompted"] == 1
    assert calls["sent"] == [ws.CONSENT_PROMPT]
    assert calls["created"] == 0, "a cold inbound must NEVER create a tenant (DPDP)"


def test_consent_reply_creates_tenant_and_kicks_journey(monkeypatch) -> None:
    calls = _wire(monkeypatch, session=_pending(), classify=lambda b, **k: "consent")
    out = ws.handle_unknown_inbound(_PHONE, "yes", "SM2")
    assert out["outcome"] == "tenant_created"
    assert calls["created"] == 1
    assert calls["consented"] == calls["tenant_id"]
    assert calls["sent"] == [ws.WELCOME_AFTER_CONSENT]
    # Finding A regression pin: the journey is STARTED with the seeded from-scratch queue
    # (a WhatsApp tenant has no draft, so an unseeded journey never asks anything) …
    assert calls["journey_started"] == {
        "tenant": calls["tenant_id"],
        "queue_fields": ["business_name", "owner_name", "business_type", "city"],
    }
    # … and the first question is kicked through the proven kickoff-token path.
    assert calls["journey"] == {"tenant": calls["tenant_id"], "body": "complete setup"}


def test_declined_reply_acks_once(monkeypatch) -> None:
    calls = _wire(monkeypatch, session=_pending(), classify=lambda b, **k: "declined")
    out = ws.handle_unknown_inbound(_PHONE, "not interested", "SM3")
    assert out["outcome"] == "declined"
    assert calls["status"] == "declined"
    assert calls["sent"] == [ws.DECLINED_ACK]
    assert calls["created"] == 0


def test_declined_session_stays_silent_forever(monkeypatch) -> None:
    session = {"id": "s1", "status": "declined", "consent_prompt_count": 1,
               "last_prompt_at": datetime.now(timezone.utc), "tenant_id": None}
    calls = _wire(monkeypatch, session=session,
                  classify=lambda b, **k: pytest.fail("a declined session must not classify"))
    out = ws.handle_unknown_inbound(_PHONE, "hello again", "SM4")
    assert out["outcome"] == "declined_silent"
    assert calls["sent"] == [] and calls["prompted"] == 0


def test_unclear_within_cooldown_is_silent_and_burns_no_prompt(monkeypatch) -> None:
    calls = _wire(monkeypatch, session=_pending(prompts=1, last_prompt_age_hours=0.1),
                  classify=lambda b, **k: "unclear")
    out = ws.handle_unknown_inbound(_PHONE, "Hi", "SM5")
    assert out["outcome"] == "unclear_cooldown_silent"
    assert calls["sent"] == [] and calls["prompted"] == 0


def test_unclear_past_cooldown_reprompts(monkeypatch) -> None:
    calls = _wire(monkeypatch, session=_pending(prompts=1, last_prompt_age_hours=13.0),
                  classify=lambda b, **k: "unclear")
    out = ws.handle_unknown_inbound(_PHONE, "what is this?", "SM6")
    assert out["outcome"] == "consent_reprompted"
    assert calls["prompted"] == 1 and calls["sent"] == [ws.CONSENT_PROMPT]


def test_prompts_exhausted_expires_silently(monkeypatch) -> None:
    calls = _wire(monkeypatch, session=_pending(prompts=ws.MAX_CONSENT_PROMPTS),
                  classify=lambda b, **k: "unclear")
    out = ws.handle_unknown_inbound(_PHONE, "??", "SM7")
    assert out["outcome"] == "expired"
    assert calls["status"] == "expired" and calls["sent"] == []


def test_handler_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(ws, "purge_stale", lambda **k: 0)

    def _boom(p):
        raise RuntimeError("db down")

    monkeypatch.setattr(ws, "get_session", _boom)
    out = ws.handle_unknown_inbound(_PHONE, "Hi", "SM8")
    assert out["outcome"] == "error"


def test_no_raw_phone_in_outcomes(monkeypatch) -> None:
    """CL-390: outcome dicts (logged by the workflow) carry the hash token, never the number."""
    calls = _wire(monkeypatch, session=None)
    out = ws.handle_unknown_inbound(_PHONE, "Hi", "SM9")
    assert _PHONE not in json.dumps(out)
    assert out["phone_token"].startswith("phone_tok_")
    assert calls["sent"]  # the prompt did go out


def test_first_contact_refusal_gets_no_solicitation(monkeypatch) -> None:
    """A cold 'STOP' never receives a consent prompt — declined + silent from message one."""
    calls = _wire(monkeypatch, session=None)
    out = ws.handle_unknown_inbound(_PHONE, "STOP", "SM10")
    assert out["outcome"] == "declined_silent"
    assert calls["sent"] == []
    assert calls["status"] == "declined"
    assert calls["created"] == 0
