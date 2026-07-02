"""Live-drill staging (Cowork 20260702T110000Z) — run via `railway run` (dev env injected).

1. team_welcome4 Meta approval status (en + hi) via the Twilio Content API — READ-ONLY (no send).
2. DEV_SEND_ALLOWLIST verify-BY-USE (sealed vars read 'unset' via presence tools): membership of the
   four CL-2026-07-01 Fazal-provided numbers, reported as last4-keyed booleans only.
3. CLEAR Fazal's owner number for a fresh signup (Fazal-authorized, VT-513 precedent): delete any
   tenant whose whatsapp_number/owner_phone matches, FK-ordered. Reports counts only.

Prints NO secrets, NO full numbers (last-4 only), NO env values.
"""

from __future__ import annotations

import os
import sys

import requests

# CL-2026-07-01-dev-send-allowlist — the four Fazal-PROVIDED numbers (repo ledger; not fabricated).
_ALLOWLIST_EXPECTED = [
    "+919321553267",  # owner
    "+919820463598",
    "+917738859946",
    "+919892616965",
]
_OWNER_NUMBER = "+919321553267"

_WELCOME4_SIDS = {  # .viabe/templates.md (canonical registry)
    "en": "HXc8188616b2e97b557f4c7330157c4f8f",
    "hi": "HXd8a8d5945c79c75d373d9c24edd4b183",
}


def _check_welcome4() -> bool:
    sid_ok = True
    acct = os.environ.get("TEAM_TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TEAM_TWILIO_AUTH_TOKEN")
    if not acct or not tok:
        print("[welcome4] FAIL: Twilio creds not in env")
        return False
    for lang, content_sid in _WELCOME4_SIDS.items():
        r = requests.get(
            f"https://content.twilio.com/v1/Content/{content_sid}/ApprovalRequests",
            auth=(acct, tok), timeout=30,
        )
        if r.status_code != 200:
            print(f"[welcome4 {lang}] FAIL: HTTP {r.status_code}")
            sid_ok = False
            continue
        wa = (r.json().get("whatsapp") or {})
        status, cat = wa.get("status"), wa.get("category")
        print(f"[welcome4 {lang}] approval status={status} category={cat}")
        sid_ok = sid_ok and status == "approved" and cat == "UTILITY"
    return sid_ok


def _check_allowlist() -> bool:
    raw = os.environ.get("DEV_SEND_ALLOWLIST", "")
    members = {n.strip() for n in raw.split(",") if n.strip()}
    ok = True
    for n in _ALLOWLIST_EXPECTED:
        present = n in members
        print(f"[allowlist] ..{n[-4:]}: {'present' if present else 'MISSING'}")
        ok = ok and present
    print(f"[allowlist] size={len(members)} (expected 4)")
    return ok and len(members) == 4


def _clear_owner_number() -> bool:
    import psycopg

    dsn = os.environ.get("TEAM_SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("[clear] FAIL: no DB URL in env")
        return False
    with psycopg.connect(dsn, autocommit=True) as c:
        rows = c.execute(
            "SELECT id FROM tenants WHERE whatsapp_number = %s OR owner_phone = %s",
            (_OWNER_NUMBER, _OWNER_NUMBER),
        ).fetchall()
        ids = [str(r[0]) for r in rows]
        print(f"[clear] tenants on owner number ..{_OWNER_NUMBER[-4:]}: {len(ids)}")
        # Every table FK-referencing tenants WITHOUT ON DELETE CASCADE must be cleared first —
        # discovered dynamically (tm_audit_log / pipeline_runs / l1_entities / … keep growing).
        noncascade = c.execute(
            "SELECT DISTINCT cl.relname AS tbl, att.attname AS col "
            "FROM pg_constraint con "
            "JOIN pg_class cl ON cl.oid = con.conrelid "
            "JOIN pg_attribute att ON att.attrelid = con.conrelid "
            "     AND att.attnum = ANY(con.conkey) "
            "WHERE con.contype = 'f' AND con.confrelid = 'public.tenants'::regclass "
            "  AND con.confdeltype <> 'c'",
        ).fetchall()
        for tid in ids:
            # Two passes: a non-cascading table may itself be referenced by another.
            for _pass in (1, 2):
                for tbl, col in noncascade:
                    try:
                        c.execute(f'DELETE FROM "{tbl}" WHERE "{col}" = %s', (tid,))  # noqa: S608 — catalog-derived
                    except Exception:  # noqa: BLE001 — retried on pass 2 / surfaced by the final delete
                        pass
            c.execute("DELETE FROM tenants WHERE id = %s", (tid,))
            print(f"[clear] deleted tenant {tid[:8]}… ({len(noncascade)} non-cascade tables swept)")
        left = c.execute(
            "SELECT count(*) FROM tenants WHERE whatsapp_number = %s OR owner_phone = %s",
            (_OWNER_NUMBER, _OWNER_NUMBER),
        ).fetchone()[0]
        print(f"[clear] tenants left on the number: {left}")
        return left == 0


def main() -> int:
    ok = _check_welcome4()
    ok = _check_allowlist() and ok
    ok = _clear_owner_number() and ok
    print(f"\nRESULT: {'DRILL-STAGE PASS' if ok else 'DRILL-STAGE FAIL (see above)'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
