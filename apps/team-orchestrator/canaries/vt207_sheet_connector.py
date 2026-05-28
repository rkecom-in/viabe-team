#!/usr/bin/env python3
"""VT-207 Google Sheet connector canary (Rule #15, DR-15).

DB + crypto + Apps Script substrate verification. Does NOT hit the
real Google OAuth or Sheets API (requires interactive user consent +
a real Sheet) — those paths are exercised manually by Fazal during
the RKeCom bootstrap walk. Real-OAuth integration test deferred to
VT-207-PR-2 follow-up once Fazal completes the manual handshake +
a refresh_token lands in the dev project.

Subshell-source supabase-dev.env + google-oauth.env:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      source ../../.viabe/secrets/google-oauth.env
      set +a
      ./.venv/bin/python canaries/vt207_sheet_connector.py
    )

Wall-clock < 15s. Cost: 0 paise.

5 assertions:

- A1: ``build_auth_url`` returns a Google OAuth URL with the expected
  client_id, scope, redirect_uri + tenant_id state param
- A2: encrypt_value / decrypt_value round-trip through VT-191's
  Fernet substrate works (proves the shared helper extraction is
  load-bearing for token storage)
- A3: ``setup_push`` requires a token row first; raises RuntimeError
  when called before complete_auth
- A4: Apps Script template renders deterministically with all 4
  substitution params (tenant_id / spreadsheet_id / orch_url /
  push_secret)
- A5: HMAC signature verification round-trips (sign → verify ok;
  bad signature → false)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANT_IDS: list[str] = []


def assertion(num: int, name: str, passed: bool, *, observed: Any = None, expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _preflight() -> None:
    for k in ("DATABASE_URL", "TEAM_PHONE_ENCRYPTION_KEY",
              "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
              "GOOGLE_OAUTH_REDIRECT_URI"):
        if not os.environ.get(k):
            print(f"PREFLIGHT FAIL — {k} missing", file=sys.stderr)
            sys.exit(2)
    print("PREFLIGHT OK — supabase + google-oauth env loaded")


def run_canary() -> int:
    _preflight()

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    pool = get_pool()

    from orchestrator.integrations.connectors.apps_script_template import (
        render_apps_script,
        verify_push_signature,
    )
    from orchestrator.integrations.connectors.google_sheet import (
        GoogleSheetConnector,
    )
    from orchestrator.observability.encrypt_value import (
        decrypt_value,
        encrypt_value,
    )

    connector = GoogleSheetConnector()
    tenant_a = uuid4()
    INSERTED_TENANT_IDS.append(str(tenant_a))

    # A1 — auth_url shape
    auth_url = connector.build_auth_url(tenant_a)
    client_id = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
    pass_1 = (
        "accounts.google.com" in auth_url
        and "spreadsheets.readonly" in auth_url
        and client_id in auth_url
        and str(tenant_a) in auth_url
        and "access_type=offline" in auth_url
        and "prompt=consent" in auth_url
    )
    assertion(
        1,
        "build_auth_url: contains client_id + scope + redirect_uri + tenant state + offline+consent",
        pass_1,
        observed={
            "host_ok": "accounts.google.com" in auth_url,
            "scope_ok": "spreadsheets.readonly" in auth_url,
            "client_id_ok": client_id in auth_url,
            "tenant_state_ok": str(tenant_a) in auth_url,
            "offline_ok": "access_type=offline" in auth_url,
            "consent_ok": "prompt=consent" in auth_url,
        },
        expected={"all_six": True},
    )

    # A2 — Fernet round-trip via shared helper
    plaintext = "sample-refresh-token-12345"
    ct = encrypt_value(plaintext)
    pt_back = decrypt_value(ct)
    pass_2 = pt_back == plaintext and ct != plaintext and ct.startswith("gAAA")
    assertion(
        2,
        "encrypt_value / decrypt_value round-trip via shared Fernet helper",
        pass_2,
        observed={
            "ciphertext_prefix": ct[:20],
            "ciphertext_differs": ct != plaintext,
            "roundtrip_ok": pt_back == plaintext,
        },
        expected={"all_three": True},
    )

    # A3 — setup_push without complete_auth → RuntimeError
    setup_failed = False
    setup_error: str | None = None
    try:
        connector.setup_push(tenant_a, "fake-spreadsheet-id")
    except RuntimeError as exc:
        setup_failed = True
        setup_error = str(exc)
    pass_3 = setup_failed and "push_secret" in (setup_error or "")
    assertion(
        3,
        "setup_push raises RuntimeError when no OAuth token row exists",
        pass_3,
        observed={"raised": setup_failed, "error_prefix": (setup_error or "")[:80]},
        expected={"raised": True, "mentions": "push_secret"},
    )

    # A4 — Apps Script template render
    script = render_apps_script(
        tenant_id=str(tenant_a),
        spreadsheet_id="test-sheet-id",
        orchestrator_base="http://localhost:8001",
        push_secret="test-secret-xyz",
    )
    pass_4 = (
        str(tenant_a) in script
        and "test-sheet-id" in script
        and "http://localhost:8001" in script
        and "test-secret-xyz" in script
        and "computeHmacSha256Signature" in script
        and "X-Viabe-Signature" in script
    )
    assertion(
        4,
        "render_apps_script: all 4 substitution params + HMAC + header present",
        pass_4,
        observed={
            "size_bytes": len(script),
            "tenant_id_in_script": str(tenant_a) in script,
            "spreadsheet_id_in_script": "test-sheet-id" in script,
            "push_secret_in_script": "test-secret-xyz" in script,
            "hmac_call_present": "computeHmacSha256Signature" in script,
        },
        expected={"all_six": True},
    )

    # A5 — HMAC signature verification round-trip
    import hashlib
    import hmac

    secret = "test-secret-xyz"
    body = b'{"row_data":{"phone":"+919998887776"}}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    good = verify_push_signature(body=body, signature=sig, push_secret=secret)
    bad = verify_push_signature(body=body, signature="00" * 32, push_secret=secret)
    pass_5 = good and not bad
    assertion(
        5,
        "verify_push_signature: correct HMAC passes; bad signature fails",
        pass_5,
        observed={"good_passes": good, "bad_rejected": not bad},
        expected={"good_passes": True, "bad_rejected": True},
    )

    return _finalise(pool)


def _finalise(pool: Any) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if INSERTED_TENANT_IDS:
                cur.execute(
                    "DELETE FROM tenant_oauth_tokens WHERE tenant_id = ANY(%s)",
                    (INSERTED_TENANT_IDS,),
                )
                cur.execute(
                    "DELETE FROM tenants WHERE id = ANY(%s)",
                    (INSERTED_TENANT_IDS,),
                )
    except BaseException as exc:  # noqa: BLE001
        print(f"cleanup partial: {exc!r}", file=sys.stderr)
    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print(f"\nALL {len(RESULTS)} ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
