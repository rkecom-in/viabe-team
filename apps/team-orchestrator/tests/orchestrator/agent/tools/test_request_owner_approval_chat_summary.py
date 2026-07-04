"""VT-594 (post-review restructure) — arm_pause_request chat-summary ordering.

The review's Blocker 3 fix moved the in-chat plan-summary send OUT of
collapse.py (which fired it unconditionally, before the run knew whether the
approval gate would even be reached — a double-send/contradiction risk on the
queue_busy/send_failed/budget-skip cases) and INTO
``request_owner_approval.arm_pause_request``, which is the one place that
actually knows the send is about to happen (not a resume re-execution, not a
0b queue-busy refusal).

Pure-function-style unit tests: DB access is monkeypatched via
``arm_pause_request``'s own dependency-injection points (``conn_factory``,
``send_fn``) plus the ``PendingApprovalsWrapper`` class methods, so these run
with NO real Postgres — no ``DATABASE_URL`` needed.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("langgraph")

from orchestrator.agent.tools import request_owner_approval as roa  # noqa: E402


class _FakeConn:
    """Minimal stand-in — only ``rollback`` is ever invoked on the conn
    directly (the UniqueViolation race-loser path); everything else routes
    through the monkeypatched wrapper methods."""

    def rollback(self) -> None:
        pass


@contextlib.contextmanager
def _fake_conn_factory(_tenant_id):
    yield _FakeConn()


def _payload(**overrides) -> roa.RequestOwnerApprovalInput:
    base: dict = dict(
        tenant_id=uuid4(),
        run_id=uuid4(),
        approval_type="campaign_send",
        summary="Approve sending a recovery campaign to 6 customers?",
        chat_summary={"en": "plan summary en", "hi": "plan summary hi"},
    )
    base.update(overrides)
    return roa.RequestOwnerApprovalInput(**base)


def _patch_common(
    monkeypatch,
    *,
    open_for_run: dict | None = None,
    open_for_tenant: dict | None = None,
    insert_raises: Exception | None = None,
    owner_phone: str | None = "+10000000000",
) -> None:
    monkeypatch.setattr(
        roa, "_find_open_approval", lambda conn, tenant_id, run_id: open_for_run
    )
    monkeypatch.setattr(
        roa.PendingApprovalsWrapper,
        "find_open_for_tenant",
        lambda self, tenant_id, conn=None: open_for_tenant,
    )

    def _insert(self, tenant_id, row, conn=None):
        if insert_raises is not None:
            raise insert_raises
        return {"id": row["id"]}

    monkeypatch.setattr(roa.PendingApprovalsWrapper, "insert", _insert)
    monkeypatch.setattr(roa, "_resolve_owner_phone", lambda conn, tenant_id: owner_phone)
    monkeypatch.setattr(
        "orchestrator.observability.tm_audit.emit_tm_audit", lambda **k: None
    )


def _ok_send_fn(order: list[str]):
    def _send_fn(tenant_id, template_name, params, *, recipient_phone):
        order.append("template")
        return SimpleNamespace(success=True, message_sid="SM_TEST")

    return _send_fn


# ---------------------------------------------------------------------------
# Ordering: summary before template
# ---------------------------------------------------------------------------


def test_chat_summary_sent_before_template(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale",
        lambda tenant_id: "en",
    )
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack",
        lambda tenant_id, recipient, body: order.append("summary") or True,
    )
    _patch_common(monkeypatch)

    result = roa.arm_pause_request(
        _payload(), conn_factory=_fake_conn_factory, send_fn=_ok_send_fn(order)
    )

    assert result.status == "armed"
    assert order == ["summary", "template"], (
        "the chat summary must send BEFORE the approval template"
    )


def test_chat_summary_uses_hindi_variant_for_hindi_locale(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale",
        lambda tenant_id: "hi",
    )
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack",
        lambda tenant_id, recipient, body: sent.append(body) or True,
    )
    _patch_common(monkeypatch)

    roa.arm_pause_request(
        _payload(), conn_factory=_fake_conn_factory, send_fn=_ok_send_fn([])
    )

    assert sent == ["plan summary hi"]


# ---------------------------------------------------------------------------
# Not sent on refusal / dry_run / resume-idempotency / no chat_summary
# ---------------------------------------------------------------------------


def test_chat_summary_not_sent_on_0b_queue_busy_refusal(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True,
    )
    _patch_common(monkeypatch, open_for_tenant={"id": str(uuid4()), "approval_type": "campaign_send"})

    result = roa.arm_pause_request(
        _payload(), conn_factory=_fake_conn_factory, send_fn=_ok_send_fn([])
    )

    assert result.status == "refused"
    assert called["n"] == 0


def test_chat_summary_not_sent_on_resume_idempotency_reexec(monkeypatch):
    """Step 0 (an OPEN approval already exists for this run) returns EARLY —
    a resume re-execution must not re-send the summary."""
    called = {"n": 0}
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True,
    )
    _patch_common(monkeypatch, open_for_run={"id": str(uuid4())})

    result = roa.arm_pause_request(
        _payload(), conn_factory=_fake_conn_factory, send_fn=_ok_send_fn([])
    )

    assert result.status == "armed"
    assert called["n"] == 0


def test_chat_summary_not_sent_on_dry_run(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True,
    )
    _patch_common(monkeypatch)

    result = roa.arm_pause_request(
        _payload(), conn_factory=_fake_conn_factory, send_fn=_ok_send_fn([]), dry_run=True
    )

    assert result.status == "armed"
    assert called["n"] == 0


def test_no_chat_summary_built_sends_nothing_no_behavior_change(monkeypatch):
    """Every existing caller that doesn't build a chat_summary (agent_customer_
    send, business_impact_choke, autonomy) must see zero new behavior."""
    called = {"n": 0}
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True,
    )
    _patch_common(monkeypatch)

    result = roa.arm_pause_request(
        _payload(chat_summary=None),
        conn_factory=_fake_conn_factory,
        send_fn=_ok_send_fn([]),
    )

    assert result.status == "armed"
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Fail-soft: a summary-send failure never blocks the arm
# ---------------------------------------------------------------------------


def test_arm_still_succeeds_when_summary_send_raises(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale",
        lambda tenant_id: "en",
    )

    def _boom(*a, **k):
        raise RuntimeError("summary send exploded")

    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack", _boom
    )
    _patch_common(monkeypatch)

    result = roa.arm_pause_request(
        _payload(), conn_factory=_fake_conn_factory, send_fn=_ok_send_fn([])
    )

    assert result.status == "armed"


# ---------------------------------------------------------------------------
# _resolve_owner_phone — priority order (VT-594 review test gap #4)
# ---------------------------------------------------------------------------


def test_resolve_owner_phone_prefers_owner_phone_over_whatsapp_number():
    class _Conn:
        def execute(self, *a, **k):
            return SimpleNamespace(
                fetchone=lambda: {"owner_phone": "+91OWNERPHONE", "whatsapp_number": "+91WANUMBER"}
            )

    assert roa._resolve_owner_phone(_Conn(), uuid4()) == "+91OWNERPHONE"


def test_resolve_owner_phone_falls_back_to_whatsapp_number_when_absent():
    class _Conn:
        def execute(self, *a, **k):
            return SimpleNamespace(
                fetchone=lambda: {"owner_phone": None, "whatsapp_number": "+91WANUMBER"}
            )

    assert roa._resolve_owner_phone(_Conn(), uuid4()) == "+91WANUMBER"


def test_resolve_owner_phone_no_row_returns_none():
    class _Conn:
        def execute(self, *a, **k):
            return SimpleNamespace(fetchone=lambda: None)

    assert roa._resolve_owner_phone(_Conn(), uuid4()) is None
