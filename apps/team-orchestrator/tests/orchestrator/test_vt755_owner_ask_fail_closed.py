"""VT-755 scopes 0+1 under ruling D-A — the emitter, and what may reach an owner.

RULING D-A (Fazal 2026-08-15, CL-2026-08-15-three-m2b-rulings): **fail-closed; raw model remediation
never reaches an owner; not-from-closed-vocabulary → an honest "I need X from you", emitted through
the SINGLE choke.**

WHAT WAS REACHING THE OWNER. `review._insufficient_data` built the owner's message by joining
`MissingDataItem.suggested_remediation` — `str = Field(..., min_length=1)`, free text the model
writes for an ENGINEERING audience — into "Could you help with this: …?". That is how "backfill the
customer table" gets sent to a shop owner in Hinglish.

THE SOURCE SWAP THAT MAKES FAIL-CLOSED BUILDABLE. The row spent a day looking for a structural signal
ON THE MODEL'S OUTPUT to branch on, and correctly concluded there is none (`category` is free text
too). But the model's account of what is missing is exactly the thing that may not reach the owner —
so classifying it was never the right question. The owner-facing text is now composed from the
TENANT'S OWN STATE (source connected? customers? purchase history?): three deterministic DB facts,
one sentence each, written by us. Genuinely closed, and checkable.

WHY THE LADDER IS ORDERED: the answers nest. "No customers" is uninformative when nothing is
connected, and "no purchase history" is uninformative when there are no customers. The owner is asked
for the ONE thing that unblocks the next question.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from orchestrator.manager import owner_ask  # noqa: E402

_TENANT = "33333333-4444-5555-6666-777777777777"


class _Customers:
    def __init__(self, total):
        self._total = total

    def count_all(self, _t):
        return self._total


def _wire(monkeypatch, *, connected: bool, total: int, has_base: bool):
    import orchestrator.db.wrappers as w

    monkeypatch.setattr(w, "CustomersWrapper", lambda: _Customers(total))
    import orchestrator.integrations.connection_truth as ct

    monkeypatch.setattr(ct, "customer_data_source_connected", lambda _t: connected)
    import orchestrator.owner_inputs.status_query as sq

    monkeypatch.setattr(sq, "_lapsed_stats", lambda _cw, _t: (has_base, 0))


# --- the closed vocabulary ----------------------------------------------------------------------


def test_nothing_connected_asks_for_a_connection(monkeypatch):
    _wire(monkeypatch, connected=False, total=0, has_base=False)
    text = owner_ask.compose_owner_need(_TENANT)
    assert "isn't connected" in text and "Connect a source" in text


def test_connected_but_empty_asks_about_the_sync_not_a_connection(monkeypatch):
    """The ladder's point: telling a connected owner to "connect a source" is the wrong ask and reads
    as the Manager not knowing its own state."""
    _wire(monkeypatch, connected=True, total=0, has_base=False)
    text = owner_ask.compose_owner_need(_TENANT)
    assert "connected" in text and "check the sync" in text
    assert "Connect a source" not in text


def test_customers_but_no_history_asks_where_the_sales_history_lives(monkeypatch):
    _wire(monkeypatch, connected=True, total=40, has_base=False)
    text = owner_ask.compose_owner_need(_TENANT)
    assert "no purchase history" in text


def test_everything_present_asks_HONESTLY_rather_than_asserting(monkeypatch):
    """Whatever the specialist found missing is not one of the three things an owner can hand us.
    Guessing a cause here would be a confident claim about the owner's data we did not verify."""
    _wire(monkeypatch, connected=True, total=40, has_base=True)
    text = owner_ask.compose_owner_need(_TENANT)
    assert "rather ask than guess" in text


def test_an_unreadable_state_falls_to_the_honest_unknown(monkeypatch):
    import orchestrator.integrations.connection_truth as ct

    def _boom(_t):
        raise RuntimeError("db down")

    monkeypatch.setattr(ct, "customer_data_source_connected", _boom)
    text = owner_ask.compose_owner_need(_TENANT)
    assert "rather ask than guess" in text, "an unverifiable state produced a confident claim"


def test_NO_model_prose_can_reach_the_owner_through_this_path():
    """The row in one assertion: every sentence the owner can receive is a literal in our source.
    A composer that could interpolate anything from the plan would reopen the leak."""
    import pathlib

    src = pathlib.Path(owner_ask.__file__).read_text()
    composer = src[src.index("def compose_owner_need"):src.index("def _lapsed_base")]
    assert "suggested_remediation" not in composer
    assert "f\"" not in composer.replace('f"insufficient', ''), (
        "compose_owner_need interpolates — every owner-facing sentence must be a fixed literal"
    )


def test_the_review_composer_no_longer_splices_remediation(monkeypatch):
    """Pin it at the SITE too, not only in the new module — the defect was in review.py and a
    revert there would be invisible to the tests above."""
    import pathlib

    from orchestrator.manager import review

    src = pathlib.Path(review.__file__).read_text()
    body = src[src.index("remediations = ["):src.index("# No remediation the owner could act on")]
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert "compose_owner_need(tenant_id)" in code
    assert "Could you help with this" not in code, "the model-prose owner question came back"
    assert "suggested_remediation" not in code.split("owner_question=")[-1], (
        "the owner_question argument reaches the model's remediation text again"
    )


# --- the emitter --------------------------------------------------------------------------------


def test_delivery_stamps_only_after_a_REAL_send(monkeypatch):
    calls = {"sent": 0, "stamped": 0}
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack",
        lambda t, r, b: calls.__setitem__("sent", calls["sent"] + 1) or True,
    )
    monkeypatch.setattr(owner_ask, "_owner_phone", lambda _t: "+919321553267")
    from orchestrator.manager import pending_questions

    monkeypatch.setattr(
        pending_questions, "mark_delivered",
        lambda t, q: calls.__setitem__("stamped", calls["stamped"] + 1) or True,
    )
    assert owner_ask.deliver_pending_question(_TENANT, "q1", "which cohort?") is True
    assert calls == {"sent": 1, "stamped": 1}


def test_a_FAILED_send_never_stamps_delivered(monkeypatch):
    """`delivered_at` is the fact that makes a question answerable (scope 0b). Stamping it
    optimistically would recreate the very defect it closes: an owner's next message consumed as the
    answer to a question they never saw."""
    stamped: list = []
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack", lambda t, r, b: False
    )
    monkeypatch.setattr(owner_ask, "_owner_phone", lambda _t: "+919321553267")
    from orchestrator.manager import pending_questions

    monkeypatch.setattr(pending_questions, "mark_delivered", lambda t, q: stamped.append(q))
    assert owner_ask.deliver_pending_question(_TENANT, "q1", "which cohort?") is False
    assert stamped == []


def test_no_owner_phone_is_a_handled_outcome_not_a_crash(monkeypatch):
    monkeypatch.setattr(owner_ask, "_owner_phone", lambda _t: None)
    assert owner_ask.deliver_pending_question(_TENANT, "q1", "text") is False


def test_a_raising_transport_is_a_handled_outcome_too(monkeypatch):
    def _boom(t, r, b):
        raise RuntimeError("twilio down")

    monkeypatch.setattr("orchestrator.owner_surface.freeform_acks.send_freeform_ack", _boom)
    monkeypatch.setattr(owner_ask, "_owner_phone", lambda _t: "+919321553267")
    assert owner_ask.deliver_pending_question(_TENANT, "q1", "text") is False


def test_the_emitter_uses_the_SINGLE_choke_and_not_a_second_send_path():
    """NORTH-STAR: the Manager is ONE voice. A second transport call here would make the ask sound
    like a different system — and would skip the conversation-log recording the choke performs."""
    import pathlib

    import ast

    tree = ast.parse(pathlib.Path(owner_ask.__file__).read_text())
    called = {
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    imported = {
        alias.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for alias in n.names
    }
    assert "send_freeform_ack" in called, "the emitter does not go through the choke at all"
    assert not (called | imported) & {"send_freeform_message", "send_interactive_message"}, (
        "the emitter reaches PAST the choke to the transport — a second send path makes the ask "
        "sound like a different system and skips the conversation-log recording the choke performs"
    )
