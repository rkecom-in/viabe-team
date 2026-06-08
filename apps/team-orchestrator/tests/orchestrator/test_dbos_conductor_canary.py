"""VT-161 — gated LIVE canary: the orchestrator's DBOSConfig actually binds to DBOS Conductor.

Skipped unless DBOS_LIVE_CANARY=1 AND DBOS_CONDUCTOR_KEY is set (real egress to the Conductor
websocket + a real Postgres via DATABASE_URL/TEAM_SUPABASE_DB_URL). Asserts the websocket connects
under the env-driven app name — the VT-161 close condition. Fail-not-skip when enabled (Rule #15).

The app name MUST match the name registered on console.dbos.dev (the bind URL is
.../websocket/<app_name>/<key>); a mismatch leaves the console app UNAVAILABLE. This canary catches
exactly that.
"""

from __future__ import annotations

import logging
import os
import time

import pytest

pytest.importorskip("dbos")

pytestmark = pytest.mark.skipif(
    os.environ.get("DBOS_LIVE_CANARY") != "1" or not os.environ.get("DBOS_CONDUCTOR_KEY"),
    reason="DBOS_LIVE_CANARY!=1 or DBOS_CONDUCTOR_KEY absent — gated post-egress Conductor canary",
)


def test_real_conductor_connection_binds():
    from dbos import DBOS

    from dbos_config import _build_dbos_config, get_database_url

    records: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                records.append(record.getMessage())
            except Exception:  # noqa: BLE001 — capture must never break the canary
                pass

    dbos_logger = logging.getLogger("dbos")
    handler = _Cap()
    dbos_logger.addHandler(handler)
    dbos_logger.setLevel(logging.INFO)

    cfg = _build_dbos_config(get_database_url())
    assert "conductor_key" in cfg, "DBOS_CONDUCTOR_KEY present must wire conductor_key into the config"

    DBOS(config=cfg)
    try:
        DBOS.launch()
        deadline = time.time() + 25.0
        connected = False
        while time.time() < deadline:
            if any("Connected to DBOS conductor" in m for m in records):
                connected = True
                break
            time.sleep(0.5)
        assert connected, (
            f"Conductor websocket did not connect for app={cfg['name']!r} within 25s — the console app "
            f"stays UNAVAILABLE. Check: app-name mismatch vs the registered Conductor app? key rejected? "
            f"egress to the Conductor websocket blocked? Conductor log lines seen: "
            f"{[m for m in records if 'onductor' in m]}"
        )
    finally:
        dbos_logger.removeHandler(handler)
        DBOS.destroy()
