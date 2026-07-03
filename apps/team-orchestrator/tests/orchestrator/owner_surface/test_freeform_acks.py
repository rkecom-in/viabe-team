"""VT-349 — free-form in-window owner acks (the 3 rewired sites + the shared module)."""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.owner_surface import freeform_acks as fa  # noqa: E402


# ----------------------------- pure: bilingual copy --------------------------------------
def test_ack_body_bilingual_and_fallback() -> None:
    en = fa.ack_body("support_handoff", "en", ref="run-7")
    hi = fa.ack_body("support_handoff", "hi", ref="run-7")
    assert "run-7" in en and "customer service representative" in en
    assert "run-7" in hi and "ग्राहक सेवा" in hi
    assert "2,499" in fa.ack_body("refund_processing", "en", amt="2,499")
    assert "2,499" in fa.ack_body("refund_processing", "hi", amt="2,499")
    # unknown locale → en
    assert fa.ack_body("support_handoff", "xx", ref="r") == fa.ack_body("support_handoff", "en", ref="r")


# ----------------------------- send_freeform_ack: best-effort, fail-safe -----------------
def test_send_freeform_ack_sends(monkeypatch) -> None:
    seen: dict[str, str] = {}
    # VT-579: send_freeform_ack now passes tenant_id + surface so the transport records the owner turn.
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.send_freeform_message",
        lambda body, phone, **kw: (seen.update(body=body, phone=phone, **kw), "SM1")[1],
    )
    assert fa.send_freeform_ack(uuid4(), "+919811111111", "hi there") is True
    assert seen["body"] == "hi there"
    assert seen["phone"] == "+919811111111"
    assert seen["surface"] == "manager"
    assert "tenant_id" in seen


def test_send_freeform_ack_no_phone_skips() -> None:
    assert fa.send_freeform_ack(uuid4(), None, "body") is False


@pytest.mark.parametrize("code", [63016, 99999])
def test_send_freeform_ack_swallows_errors(monkeypatch, code) -> None:
    """A window-closed (63016) OR any other send error is swallowed (returns False, no raise) —
    the owner-action already landed and must not be unwound."""

    class _Exc(Exception):
        def __init__(self) -> None:
            self.code = code

    def _boom(body, phone):  # noqa: ANN001
        raise _Exc()

    monkeypatch.setattr("orchestrator.utils.twilio_send.send_freeform_message", _boom)
    assert fa.send_freeform_ack(uuid4(), "+919811111111", "body") is False  # no raise


# ----------------------------- the 3 sites send FREE-FORM (not templates) ----------------
def test_support_handoff_sends_freeform_bilingual(monkeypatch) -> None:
    import orchestrator.owner_surface.support_bot as sb

    seen: dict[str, str] = {}
    monkeypatch.setattr(fa, "resolve_owner_locale", lambda t: "hi")
    monkeypatch.setattr(fa, "send_freeform_ack", lambda t, p, body: seen.update(body=body, phone=p))
    sb._send_handoff_ack(uuid4(), "+919811111111", "run-42")
    assert "run-42" in seen["body"] and "ग्राहक सेवा" in seen["body"]  # hi copy + ref


def test_edge_ack_sends_freeform_handler_text(monkeypatch) -> None:
    import orchestrator.edge_cases_router as ecr

    seen: dict[str, str] = {}
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack",
        lambda t, p, body: seen.update(body=body),
    )
    ecr._send_edge_ack(uuid4(), "+919811111111", "You're excluded from campaigns.")
    assert seen["body"] == "You're excluded from campaigns."  # handler text sent as-is


# ----------------------------- DB: owner-locale resolution -------------------------------
@pytest.mark.integration
def test_resolve_owner_locale(_dbpool) -> None:
    def _seed(lang: str | None) -> str:
        tid = uuid4()
        with _dbpool.connection() as conn:
            conn.execute(
                "INSERT INTO tenants (id, business_name, plan_tier, phase, preferred_language) "
                "VALUES (%s, 't', 'standard', 'onboarding', %s)",
                (str(tid), lang),
            )
        return str(tid)

    assert fa.resolve_owner_locale(_seed("hi")) == "hi"
    assert fa.resolve_owner_locale(_seed("en")) == "en"
    assert fa.resolve_owner_locale(_seed(None)) == "en"  # COALESCE → en
    assert fa.resolve_owner_locale(uuid4()) == "en"  # missing tenant → en
