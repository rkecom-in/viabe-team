"""VT-489 — volume_spike alert: exclude mocked sends (b) + dev-aware paging (c).

The VT-474 anomaly detector correctly caught a send-safety re-drive burst on the
live dev tenant 63211ce5, but then repeat-paged Fazal on the ViabeOps Telegram for
DEV/TEST volume. The fix (NOT a weakening of the prod detector):

  (b) A mocked dev send (VT-476 dev_send_guard → ``MKDEV…`` SID) is NOT a real send.
      A run-volume spike whose ENTIRE outbound activity was mocked is a dev/test
      artifact, not a real-send alarm → ``detect_slow_triggers`` does NOT emit
      ``volume_spike`` (``_volume_is_mock_only``). A REAL send in the window still
      fires it. FAIL-SAFE: uncertain → alert.

  (c) On a non-prod env (``EXPECTED_ENV != prod``, VT-362 sentinel) OR a known
      canary tenant, the alert is DEV-ROUTED — DEV bot only, NEVER the ViabeOps OPS
      channel + never real email (``is_dev_routed`` + ``dispatch._dispatch_telegram``).
      On ``EXPECTED_ENV=prod`` a real volume spike STILL pages the OPS channel.

Live Postgres (DATABASE_URL). The leaf ``send_telegram`` / ``send_resend_email`` are
monkeypatched to capture recipients (no live Telegram/Resend). CL-422 synthetic data.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-489 volume_spike dev-aware tests skipped",
)


@pytest.fixture(scope="module")
def pool():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


# --- helpers ---------------------------------------------------------------


def _tenant(pool) -> str:  # type: ignore[no-untyped-def]
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"vt489-{tid[:8]}"),
        )
    return tid


def _baseline(pool, tid: str, volume_per_hour: int) -> None:  # type: ignore[no-untyped-def]
    """Pin a low hourly-volume baseline so a burst trips the 3× volume_spike gate."""
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenant_alert_baselines "
            "(tenant_id, volume_per_hour, dispatches_sampled) VALUES (%s, %s, 100) "
            "ON CONFLICT (tenant_id) DO UPDATE SET volume_per_hour = EXCLUDED.volume_per_hour",
            (tid, volume_per_hour),
        )


def _runs(pool, tid: str, n: int) -> None:  # type: ignore[no-untyped-def]
    """Open ``n`` recent twilio_inbound runs in the last hour (the volume window)."""
    with pool.connection() as conn:
        for _ in range(n):
            conn.execute(
                "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
                "VALUES (%s, %s, 'twilio_inbound', 'completed')",
                (str(uuid4()), tid),
            )


def _send(pool, tid: str, *, sid: str | None, status: str = "sent") -> None:  # type: ignore[no-untyped-def]
    """Record a send-ledger row. ``sid`` starting MKDEV = a VT-476 mocked dev send."""
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO send_idempotency_keys "
            "(tenant_id, idempotency_key, message_sid, send_status) VALUES (%s, %s, %s, %s)",
            (tid, uuid4().hex, sid, status),
        )


# --- (b) mocked sends excluded from the volume metric ----------------------


def test_mocked_only_window_does_not_fire_volume_spike(pool):
    """A run-volume burst whose outbound sends were ALL mocked (MKDEV) does NOT
    emit volume_spike — the exact 63211ce5 re-drive shape (dev guard mocks every
    send)."""
    from orchestrator.alerts.triggers import detect_slow_triggers

    tid = _tenant(pool)
    _baseline(pool, tid, volume_per_hour=1)   # baseline 1/h → 3× gate = 3
    _runs(pool, tid, 21)                       # 21 inbound runs (the observed burst)
    for _ in range(13):                        # all outbound sends were MOCKED
        _send(pool, tid, sid=f"MKDEV{uuid4().hex[:27]}")

    kinds = [t.trigger_kind for t in detect_slow_triggers(UUID(tid))]
    assert "volume_spike" not in kinds


def test_real_send_in_window_still_fires_volume_spike(pool):
    """A burst with at least one REAL (non-MKDEV) send DOES fire — the metric is
    NOT weakened for genuine real-send volume."""
    from orchestrator.alerts.triggers import detect_slow_triggers

    tid = _tenant(pool)
    _baseline(pool, tid, volume_per_hour=1)
    _runs(pool, tid, 21)
    _send(pool, tid, sid=f"MKDEV{uuid4().hex[:27]}")   # one mocked
    _send(pool, tid, sid=f"SM{uuid4().hex[:30]}")      # AND one real send

    kinds = [t.trigger_kind for t in detect_slow_triggers(UUID(tid))]
    assert "volume_spike" in kinds


def test_no_sends_at_all_still_fires_volume_spike(pool):
    """FAIL-SAFE: a burst with NO send-ledger rows at all is NOT mock-only (no mock
    found) → the guard does not suppress → volume_spike still fires (prefer to
    alert when uncertain)."""
    from orchestrator.alerts.triggers import detect_slow_triggers

    tid = _tenant(pool)
    _baseline(pool, tid, volume_per_hour=1)
    _runs(pool, tid, 21)
    # no send rows

    kinds = [t.trigger_kind for t in detect_slow_triggers(UUID(tid))]
    assert "volume_spike" in kinds


def test_volume_is_mock_only_unit(pool):
    """Direct unit of the helper: mock-only → True; any real send → False;
    no sends → False (fail-safe)."""
    from orchestrator.alerts.triggers import _volume_is_mock_only

    t_mock = _tenant(pool)
    _send(pool, t_mock, sid=f"MKDEV{uuid4().hex[:27]}")
    assert _volume_is_mock_only(UUID(t_mock)) is True

    t_real = _tenant(pool)
    _send(pool, t_real, sid=f"MKDEV{uuid4().hex[:27]}")
    _send(pool, t_real, sid=f"SM{uuid4().hex[:30]}")
    assert _volume_is_mock_only(UUID(t_real)) is False

    t_none = _tenant(pool)
    assert _volume_is_mock_only(UUID(t_none)) is False


# --- (c) dev-aware paging --------------------------------------------------


def _capture(monkeypatch):  # type: ignore[no-untyped-def]
    """Capture the leaf telegram/email sends; return (telegram_chats, email_calls)."""
    tg_chats: list[str] = []
    email_calls: list[tuple] = []

    async def _fake_tg(bot_token: str, chat_id: str, text: str) -> bool:
        tg_chats.append(chat_id)
        return True

    async def _fake_email(*a, **k) -> bool:
        email_calls.append((a, k))
        return True

    from orchestrator.alerts import dispatch as _d

    monkeypatch.setattr(_d, "send_telegram", _fake_tg)
    monkeypatch.setattr(_d, "send_resend_email", _fake_email)
    return tg_chats, email_calls


def _volume_trigger(tid: str):  # type: ignore[no-untyped-def]
    from orchestrator.alerts.triggers import Trigger

    return Trigger(
        tenant_id=UUID(tid), trigger_kind="volume_spike", severity="warning",
        message_text="hourly volume spike",
    )


def _set_routing_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", "ops-token")
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "OPS-CHAT")
    monkeypatch.setenv("TELEGRAM_DEV_BOT_TOKEN", "dev-token")
    monkeypatch.setenv("TELEGRAM_DEV_CHAT_ID", "DEV-CHAT")
    monkeypatch.delenv("TEAM_CANARY_TENANT_IDS", raising=False)


def test_dev_env_volume_spike_does_not_page_ops(pool, monkeypatch):
    """On a NON-prod env, a volume_spike on a non-canary tenant routes to the DEV
    bot only — NEVER the ViabeOps OPS channel. This is the core 63211ce5 fix."""
    from orchestrator.alerts.dispatch import dispatch_alert

    tid = _tenant(pool)
    _set_routing_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    tg_chats, _ = _capture(monkeypatch)

    dispatch_alert(_volume_trigger(tid))

    assert set(tg_chats) == {"DEV-CHAT"}   # DEV bot only
    assert "OPS-CHAT" not in tg_chats      # never pages Fazal on ViabeOps


def test_dev_env_unset_defaults_to_dev_routing(pool, monkeypatch):
    """EXPECTED_ENV UNSET defaults to dev (VT-362 posture) → dev-routed, no OPS page."""
    from orchestrator.alerts.dispatch import dispatch_alert

    tid = _tenant(pool)
    _set_routing_env(monkeypatch)
    monkeypatch.delenv("EXPECTED_ENV", raising=False)
    tg_chats, _ = _capture(monkeypatch)

    dispatch_alert(_volume_trigger(tid))

    assert "OPS-CHAT" not in tg_chats


def test_prod_env_volume_spike_pages_ops(pool, monkeypatch):
    """On EXPECTED_ENV=prod, a real volume_spike STILL pages the ViabeOps OPS
    channel — the detector is NOT weakened for prod."""
    from orchestrator.alerts.dispatch import dispatch_alert

    tid = _tenant(pool)
    _set_routing_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    tg_chats, _ = _capture(monkeypatch)

    dispatch_alert(_volume_trigger(tid))

    assert "OPS-CHAT" in tg_chats          # prod paging intact
    assert "DEV-CHAT" not in tg_chats


def test_canary_tenant_dev_routed_even_in_prod(pool, monkeypatch):
    """A canary tenant stays DEV-routed even on prod (existing Cowork canary lock,
    preserved by is_dev_routed)."""
    from orchestrator.alerts.dispatch import dispatch_alert

    tid = _tenant(pool)
    _set_routing_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    monkeypatch.setenv("TEAM_CANARY_TENANT_IDS", tid)
    tg_chats, _ = _capture(monkeypatch)

    dispatch_alert(_volume_trigger(tid))

    assert set(tg_chats) == {"DEV-CHAT"}
    assert "OPS-CHAT" not in tg_chats


def test_is_dev_routed_unit(monkeypatch):
    """Unit: dev env → True; prod env + non-canary → False; prod + canary → True."""
    from orchestrator.alerts.dispatch import is_dev_routed

    tid = uuid4()
    monkeypatch.delenv("TEAM_CANARY_TENANT_IDS", raising=False)

    monkeypatch.setenv("EXPECTED_ENV", "dev")
    assert is_dev_routed(tid) is True

    monkeypatch.setenv("EXPECTED_ENV", "prod")
    assert is_dev_routed(tid) is False

    monkeypatch.setenv("TEAM_CANARY_TENANT_IDS", str(tid))
    assert is_dev_routed(tid) is True   # canary always dev-routed, even on prod
