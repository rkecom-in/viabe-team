"""VT-502 — route the SupportBot VT-88 escalation + VT-343 FATIGUE (and every
``alert_fazal`` caller) through the VT-489 dev-routing gate so a dev/canary/bogus
tenant NEVER pages ViabeOps (PROD ops).

ROOT CAUSE: ``owner_surface/support_bot.py`` emitted the VT-88 escalation + the
FATIGUE line to ViabeOps DIRECTLY via ``alerts.clients.alert_fazal`` — NOT through
the VT-489 ``is_dev_routed`` gate that ``alerts/dispatch.py`` already applies to
volume_spike. So the bogus re-drive tenant ``f0000bcd-…-beef`` paged PROD ops.

THE FIX (this file proves it):
  - ``alert_fazal(text, tenant_id=...)`` is now dev-aware (centralized
    ``alert_is_dev_routed`` reuses VT-489's ``is_dev_routed``). A dev/canary alert
    → DEV bot only, NEVER the ViabeOps OPS channel.
  - PROD INTACT: a real (non-canary) tenant's escalation STILL pages ViabeOps and
    the FATIGUE business-stability line STILL fires on prod.
  - scrub_pii no longer digit-mangles a tenant/run UUID (a UUID is not PII).

Pure env + monkeypatched leaf ``send_telegram`` — no live Telegram, no DB (the
escalation/FATIGUE counters are stubbed). Synthetic ids only (CL-422 / never a
real number).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("httpx")  # alerts.clients imports httpx at module load — dep-less smoke guard

import orchestrator.alerts.clients as clients  # noqa: E402 — after the dependency skip guard
from orchestrator.owner_surface import support_bot as sb  # noqa: E402

# The actual bogus re-drive tenant (confirmed in dev: a hand-crafted v4-shaped id)
# + its synthetic +1-555 owner (+15550000901, North-American reserved-for-fiction;
# NOT +91, NOT in Fazal's set — confirmed synthetic, no real number leaked).
_BOGUS_TENANT = "f0000bcd-0000-4000-8000-00000000beef"


def _set_routing_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", "ops-token")
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "OPS-CHAT")
    monkeypatch.setenv("TELEGRAM_DEV_BOT_TOKEN", "dev-token")
    monkeypatch.setenv("TELEGRAM_DEV_CHAT_ID", "DEV-CHAT")
    monkeypatch.delenv("TEAM_CANARY_TENANT_IDS", raising=False)


def _capture_leaf(monkeypatch):  # type: ignore[no-untyped-def]
    """Capture the leaf ``send_telegram`` (chat_id, text); no network. Returns the list."""
    sent: list[tuple[str, str]] = []

    async def _fake_tg(bot_token: str, chat_id: str, text: str) -> bool:
        sent.append((chat_id, text))
        return True

    monkeypatch.setattr(clients, "send_telegram", _fake_tg)
    return sent


# --- alert_is_dev_routed unit (the gate) -----------------------------------


def test_alert_is_dev_routed_global_env_arm(monkeypatch) -> None:
    """Global alert (tenant_id=None): dev-routed iff non-prod env."""
    _set_routing_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    assert clients.alert_is_dev_routed(None) is True
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    assert clients.alert_is_dev_routed(None) is False
    monkeypatch.delenv("EXPECTED_ENV", raising=False)  # unset → dev posture
    assert clients.alert_is_dev_routed(None) is True


def test_alert_is_dev_routed_tenant_scoped(monkeypatch) -> None:
    """Tenant-scoped: dev env → True; prod + non-canary → False; prod + canary → True."""
    pytest.importorskip("langgraph")  # alert_is_dev_routed(tenant) imports dispatch
    _set_routing_env(monkeypatch)
    tid = str(uuid4())

    monkeypatch.setenv("EXPECTED_ENV", "dev")
    assert clients.alert_is_dev_routed(tid) is True

    monkeypatch.setenv("EXPECTED_ENV", "prod")
    assert clients.alert_is_dev_routed(tid) is False  # real prod tenant → OPS

    monkeypatch.setenv("TEAM_CANARY_TENANT_IDS", tid)
    assert clients.alert_is_dev_routed(tid) is True  # canary dev-routed even on prod


# --- alert_fazal routing (the leaf chat the message actually lands on) ------


def test_alert_fazal_global_dev_routes_dev_bot(monkeypatch) -> None:
    _set_routing_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    sent = _capture_leaf(monkeypatch)
    clients.alert_fazal("global ops alert")  # no tenant
    assert {c for c, _ in sent} == {"DEV-CHAT"}
    assert "OPS-CHAT" not in {c for c, _ in sent}


def test_alert_fazal_global_prod_pages_ops(monkeypatch) -> None:
    """PROD global ops alert still pages ViabeOps (unchanged)."""
    _set_routing_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    sent = _capture_leaf(monkeypatch)
    clients.alert_fazal("global ops alert")
    assert {c for c, _ in sent} == {"OPS-CHAT"}


def test_alert_fazal_prod_real_tenant_pages_ops(monkeypatch) -> None:
    """PROD, real (non-canary) tenant → ViabeOps (PROD paging intact)."""
    pytest.importorskip("langgraph")
    _set_routing_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    sent = _capture_leaf(monkeypatch)
    clients.alert_fazal("tenant alert", tenant_id=str(uuid4()))
    assert {c for c, _ in sent} == {"OPS-CHAT"}


def test_alert_fazal_dev_tenant_dev_bot_only(monkeypatch) -> None:
    """DEV env, any tenant → DEV bot only, NEVER ViabeOps (the core leak fix)."""
    pytest.importorskip("langgraph")
    _set_routing_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    sent = _capture_leaf(monkeypatch)
    clients.alert_fazal("tenant alert", tenant_id=_BOGUS_TENANT)
    assert {c for c, _ in sent} == {"DEV-CHAT"}
    assert "OPS-CHAT" not in {c for c, _ in sent}


def test_alert_fazal_prod_canary_dev_bot_only(monkeypatch) -> None:
    """PROD, canary tenant → DEV bot only (Cowork canary lock preserved)."""
    pytest.importorskip("langgraph")
    _set_routing_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    monkeypatch.setenv("TEAM_CANARY_TENANT_IDS", _BOGUS_TENANT)
    sent = _capture_leaf(monkeypatch)
    clients.alert_fazal("tenant alert", tenant_id=_BOGUS_TENANT)
    assert {c for c, _ in sent} == {"DEV-CHAT"}
    assert "OPS-CHAT" not in {c for c, _ in sent}


# --- support_bot VT-88 escalation + VT-343 FATIGUE end-to-end --------------


def test_support_bot_escalation_dev_tenant_never_pages_ops(monkeypatch) -> None:
    """The bogus re-drive tenant's VT-88 escalation + a climbing FATIGUE count
    route to the DEV bot ONLY — NEVER ViabeOps. This is the exact VT-502 leak."""
    pytest.importorskip("langgraph")
    _set_routing_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setattr(sb, "_escalation_count_7d", lambda *a, **k: 12)  # FATIGUE high
    sent = _capture_leaf(monkeypatch)

    sb._alert_fazal_safe(_BOGUS_TENANT, "+15550000901", "run-redrive")

    assert {c for c, _ in sent} == {"DEV-CHAT"}
    assert "OPS-CHAT" not in {c for c, _ in sent}
    # FATIGUE is still COMPUTED (the business signal isn't suppressed) — it just
    # lands on the DEV bot on dev.
    assert any("FATIGUE" in t for _, t in sent)


def test_support_bot_escalation_prod_tenant_pages_ops_with_fatigue(monkeypatch) -> None:
    """PROD INTACT: a real prod tenant's escalation STILL pages ViabeOps and the
    FATIGUE business-stability line STILL fires."""
    pytest.importorskip("langgraph")
    _set_routing_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    monkeypatch.setattr(sb, "_escalation_count_7d", lambda *a, **k: 4)  # ≥3 → FATIGUE
    sent = _capture_leaf(monkeypatch)

    sb._alert_fazal_safe(str(uuid4()), "+919876543210", "run-prod")

    assert {c for c, _ in sent} == {"OPS-CHAT"}  # ViabeOps paged
    assert "DEV-CHAT" not in {c for c, _ in sent}
    chat, text = sent[0]
    assert "FATIGUE" in text and "proactive outreach" in text  # business signal intact
    assert "9876543210" not in text  # owner phone never raw (last-4 only)


def test_support_bot_escalation_prod_canary_never_pages_ops(monkeypatch) -> None:
    """PROD but the tenant is an explicit canary → DEV bot only (no real-ops page)."""
    pytest.importorskip("langgraph")
    _set_routing_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    monkeypatch.setenv("TEAM_CANARY_TENANT_IDS", _BOGUS_TENANT)
    monkeypatch.setattr(sb, "_escalation_count_7d", lambda *a, **k: 0)
    sent = _capture_leaf(monkeypatch)

    sb._alert_fazal_safe(_BOGUS_TENANT, "+15550000901", "run-canary")

    assert {c for c, _ in sent} == {"DEV-CHAT"}
    assert "OPS-CHAT" not in {c for c, _ in sent}


# --- audit-chain break: the OTHER global ops-emit path, now env-gated --------


def _capture_both(monkeypatch):  # type: ignore[no-untyped-def]
    """Capture leaf telegram (chat_id) + email calls for the audit-chain path."""
    tg: list[str] = []
    email: list[tuple] = []

    async def _tg(bot_token: str, chat_id: str, text: str) -> bool:
        tg.append(chat_id)
        return True

    async def _em(*a, **k) -> bool:
        email.append((a, k))
        return True

    monkeypatch.setattr(clients, "send_telegram", _tg)
    monkeypatch.setattr(clients, "send_resend_email", _em)
    return tg, email


def test_audit_chain_break_dev_routes_dev_bot_no_email(monkeypatch) -> None:
    """The global audit-chain-break alert (no per-tenant path) is dev-routing-gated:
    on a non-prod env it goes to the DEV bot ONLY and skips real email."""
    pytest.importorskip("dbos")
    from types import SimpleNamespace

    from orchestrator.scheduled_triggers import _alert_audit_chain_break

    _set_routing_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setenv("RESEND_API_KEY", "x")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "a@b.c")
    monkeypatch.setenv("RESEND_TO_EMAIL", "d@e.f")
    tg, email = _capture_both(monkeypatch)

    _alert_audit_chain_break(SimpleNamespace(broken_seq=3, reason="hash mismatch", rows_checked=5))

    assert set(tg) == {"DEV-CHAT"}
    assert "OPS-CHAT" not in tg
    assert email == []  # dev never emails real ops


def test_audit_chain_break_prod_pages_ops_and_email(monkeypatch) -> None:
    """PROD INTACT: a real chain break still pages ViabeOps + email."""
    pytest.importorskip("dbos")
    from types import SimpleNamespace

    from orchestrator.scheduled_triggers import _alert_audit_chain_break

    _set_routing_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    monkeypatch.setenv("RESEND_API_KEY", "x")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "a@b.c")
    monkeypatch.setenv("RESEND_TO_EMAIL", "d@e.f")
    tg, email = _capture_both(monkeypatch)

    _alert_audit_chain_break(SimpleNamespace(broken_seq=3, reason="hash mismatch", rows_checked=5))

    assert set(tg) == {"OPS-CHAT"}
    assert "DEV-CHAT" not in tg
    assert len(email) == 1  # OPS email fires on prod


# --- scrub_pii: a UUID is NOT PII (no digit-mangling of tenant/run ids) -----


def test_scrub_pii_keeps_tenant_uuid_intact() -> None:
    """The synthetic tenant id must survive scrub whole — not ``[REDACTED:digits]beef``."""
    from orchestrator.alerts.pii_scrub import scrub_pii

    body = f"[WARNING] volume_spike\nrun: {uuid4()}\ntenant: {_BOGUS_TENANT}"
    out = scrub_pii(body)
    assert _BOGUS_TENANT in out  # full UUID readable for the Ops Console
    assert "[REDACTED:digits]beef" not in out


def test_scrub_pii_still_redacts_real_pii_around_uuid() -> None:
    """UUID exemption does NOT relax real-PII scrubbing: a phone/bare-digit run in
    the same body is still redacted."""
    from orchestrator.alerts.pii_scrub import scrub_pii

    tid = uuid4()
    out = scrub_pii(f"tenant {tid} called +919876543210 acct 12345678")
    assert str(tid) in out  # UUID kept
    assert "+919876543210" not in out and "[REDACTED:phone]" in out
    assert "12345678" not in out and "[REDACTED:digits]" in out


def test_scrub_pii_uuid_exemption_idempotent() -> None:
    from orchestrator.alerts.pii_scrub import scrub_pii

    body = f"tenant: {_BOGUS_TENANT} phone +919811111111"
    once = scrub_pii(body)
    assert scrub_pii(once) == once
    assert _BOGUS_TENANT in once
