#!/usr/bin/env python3
"""VT-191 phone encryption-at-rest canary (Rule #15, DR-15).

Subshell-source ONLY `.viabe/secrets/supabase-dev.env`
(contains DATABASE_URL + TEAM_PHONE_ENCRYPTION_KEY per VT-191):

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt191_phone_encryption.py 2>&1 | tee /tmp/vt191-canary-evidence.log | tail -200
    )

**NO anthropic.env sourced.** Deterministic crypto substrate;
ANTHROPIC_API_KEY ABSENT at PREFLIGHT.

Wall-clock budget ≤ 45s. Cost budget: 0 paise.

9 assertions (8 from brief + Cond 1 zero-plaintext-orphans):
- A1: Fernet round-trip — encrypt + decrypt returns original
- A2: register_phone_token writes ciphertext (not plaintext) to column
- A3: resolve_phone_token returns decrypted plaintext to caller
- A4: encrypt(plain) twice → different ciphertext (Fernet IV) but
  both decrypt to original
- A5: wrong key decrypt → cryptography.fernet.InvalidToken raised
- A6: _rotate_encryption_key rotates rows; post-rotation new_key
  decrypts + old_key raises InvalidToken
- A7: Migration 028 + back-fill script idempotent (re-run = no-op
  via try-decrypt-then-encrypt-on-fail pattern; assertion runs the
  script once via subprocess + verifies newly_encrypted=0)
- A8: ANTHROPIC ABSENT preflight
- A9 (Cond 1): ZERO plaintext orphans — every row in
  phone_token_resolutions.phone_number_encrypted decrypts successfully
  under the env key. Catches the "back-fill script forgotten" failure
  mode structurally.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANT_IDS: list[str] = []
INSERTED_TOKEN_IDS: list[str] = []


def assertion(num, name, passed, *, observed=None, expected=None):
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _supabase_host():
    url = os.environ.get("DATABASE_URL", "")
    if "@" not in url:
        return "<no-host>"
    return url.split("@", 1)[1].split("/", 1)[0]


def _preflight():
    missing = [
        k for k in ("DATABASE_URL", "TEAM_PHONE_ENCRYPTION_KEY")
        if not os.environ.get(k)
    ]
    if missing:
        print(f"PREFLIGHT FAIL — missing env: {missing}", file=sys.stderr)
        sys.exit(2)
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "PREFLIGHT FAIL — ANTHROPIC_API_KEY present; this canary must NOT "
            "source anthropic.env (defense-in-depth per DR-15).",
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        f"PREFLIGHT OK — supabase: {_supabase_host()}; "
        f"TEAM_PHONE_ENCRYPTION_KEY: present; "
        f"ANTHROPIC_API_KEY: <absent — defense-in-depth>"
    )


def run_canary() -> int:
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt191-canary-salt")

    from cryptography.fernet import Fernet, InvalidToken

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool
    from orchestrator.observability.phone_tokens import (
        _rotate_encryption_key,
        decrypt_phone,
        encrypt_phone,
        register_phone_token,
        resolve_phone_token,
    )

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=8,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    pool = get_pool()

    # ----------------------------------------------------------------
    # A1: Fernet round-trip
    # ----------------------------------------------------------------
    sample_phone = f"+9199999{uuid4().hex[:5]}"
    ct = encrypt_phone(sample_phone)
    decrypted = decrypt_phone(ct)
    pass_1 = decrypted == sample_phone and ct != sample_phone
    assertion(
        1,
        "Fernet round-trip: encrypt + decrypt returns original",
        pass_1,
        observed={
            "ciphertext_prefix": ct[:40],
            "ciphertext_differs_from_plain": ct != sample_phone,
            "decrypt_returns_original": decrypted == sample_phone,
        },
        expected={"both_True": True},
    )

    # ----------------------------------------------------------------
    # A2: register_phone_token writes ciphertext (not plaintext)
    # ----------------------------------------------------------------
    tenant_a = uuid4()
    INSERTED_TENANT_IDS.append(str(tenant_a))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_a), f"canary-vt191-{tenant_a}"),
        )
    phone_a = f"+919888{uuid4().hex[:6]}"
    token_a = register_phone_token(tenant_id=tenant_a, phone_e164=phone_a)
    INSERTED_TOKEN_IDS.append(token_a)

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT phone_number_encrypted FROM phone_token_resolutions "
            "WHERE phone_token = %s",
            (token_a,),
        )
        stored = cur.fetchone()
    stored_value: str = stored["phone_number_encrypted"] if stored else ""
    decrypts_to_original = False
    try:
        decrypts_to_original = decrypt_phone(stored_value) == phone_a
    except InvalidToken:
        pass
    pass_2 = (
        stored_value != phone_a
        and stored_value.startswith("gAAA")  # Fernet base64 magic
        and decrypts_to_original
    )
    assertion(
        2,
        "register_phone_token writes ciphertext; decrypt returns original plaintext",
        pass_2,
        observed={
            "stored_prefix": stored_value[:40],
            "stored_differs_from_plain": stored_value != phone_a,
            "fernet_prefix_match": stored_value.startswith("gAAA"),
            "decrypts_to_original": decrypts_to_original,
        },
        expected={"all_True": True},
    )

    # ----------------------------------------------------------------
    # A3: resolve_phone_token returns decrypted plaintext
    # ----------------------------------------------------------------
    resolved = resolve_phone_token(
        tenant_id=tenant_a, phone_token=token_a, operator_id="canary_op"
    )
    pass_3 = resolved == phone_a
    assertion(
        3,
        "resolve_phone_token returns decrypted plaintext",
        pass_3,
        observed={"resolved": resolved, "expected_phone": phone_a},
        expected={"resolved": phone_a},
    )

    # ----------------------------------------------------------------
    # A4: encrypt(plain) twice → different ciphertext, both decrypt
    # ----------------------------------------------------------------
    ct1 = encrypt_phone(sample_phone)
    ct2 = encrypt_phone(sample_phone)
    pass_4 = (
        ct1 != ct2
        and decrypt_phone(ct1) == sample_phone
        and decrypt_phone(ct2) == sample_phone
    )
    assertion(
        4,
        "Fernet IV randomization: two encryptions differ, both decrypt to original",
        pass_4,
        observed={
            "ct1_differs_from_ct2": ct1 != ct2,
            "both_decrypt_to_original": (
                decrypt_phone(ct1) == sample_phone
                and decrypt_phone(ct2) == sample_phone
            ),
        },
        expected={"both_True": True},
    )

    # ----------------------------------------------------------------
    # A5: wrong key decrypt → InvalidToken
    # ----------------------------------------------------------------
    wrong_key = Fernet.generate_key()
    wrong_fernet = Fernet(wrong_key)
    raised_invalid = False
    try:
        wrong_fernet.decrypt(stored_value.encode())
    except InvalidToken:
        raised_invalid = True
    pass_5 = raised_invalid
    assertion(
        5,
        "Wrong key decrypt → cryptography.fernet.InvalidToken",
        pass_5,
        observed={"InvalidToken_raised": raised_invalid},
        expected={"InvalidToken_raised": True},
    )

    # ----------------------------------------------------------------
    # A6: _rotate_encryption_key works; old key fails post-rotation
    # ----------------------------------------------------------------
    # Seed a token specifically for rotation so we don't contaminate
    # the env-key state for other assertions. Rotate ALL rows (the
    # function operates on the full table), then rotate back so the
    # canary leaves dev DB in env-key state.
    old_key = os.environ["TEAM_PHONE_ENCRYPTION_KEY"]
    new_key = Fernet.generate_key().decode()
    rotated_count = _rotate_encryption_key(old_key, new_key)

    # Under new key: stored row should now decrypt with new_key
    new_fernet = Fernet(new_key.encode())
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT phone_number_encrypted FROM phone_token_resolutions "
            "WHERE phone_token = %s",
            (token_a,),
        )
        rotated_row = cur.fetchone()
    rotated_ct = rotated_row["phone_number_encrypted"] if rotated_row else ""
    decrypts_with_new = False
    old_key_raises = False
    try:
        decrypts_with_new = new_fernet.decrypt(rotated_ct.encode()).decode() == phone_a
    except InvalidToken:
        pass
    old_fernet = Fernet(old_key.encode())
    try:
        old_fernet.decrypt(rotated_ct.encode())
    except InvalidToken:
        old_key_raises = True

    # Rotate back so dev DB stays on the env key.
    rotated_back = _rotate_encryption_key(new_key, old_key)

    pass_6 = (
        rotated_count > 0
        and decrypts_with_new
        and old_key_raises
        and rotated_back == rotated_count
    )
    assertion(
        6,
        "_rotate_encryption_key: new_key decrypts; old_key raises InvalidToken; reversible",
        pass_6,
        observed={
            "rotated_count": rotated_count,
            "new_key_decrypts": decrypts_with_new,
            "old_key_raises": old_key_raises,
            "reversible_count": rotated_back,
        },
        expected={
            "rotated_count_gt": 0,
            "new_key_decrypts": True,
            "old_key_raises": True,
            "reversible": True,
        },
    )

    # ----------------------------------------------------------------
    # A7: back-fill script idempotent (newly_encrypted=0 on re-run)
    # ----------------------------------------------------------------
    repo_root = Path(__file__).resolve().parents[3]
    script_path = repo_root / "scripts" / "vt191_encrypt_existing_rows.py"
    result = subprocess.run(
        [sys.executable, str(script_path)],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    pass_7 = False
    backfill_output: Any = None
    if result.returncode == 0:
        import json as _json
        try:
            backfill_output = _json.loads(result.stdout.strip())
            pass_7 = backfill_output.get("newly_encrypted", -1) == 0
        except _json.JSONDecodeError:
            pass_7 = False
    assertion(
        7,
        "back-fill script idempotent: re-run produces newly_encrypted=0",
        pass_7,
        observed={
            "exit_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip()[:200],
            "parsed": backfill_output,
        },
        expected={"newly_encrypted": 0, "exit_code": 0},
    )

    # ----------------------------------------------------------------
    # A8: ANTHROPIC ABSENT preflight invariant
    # ----------------------------------------------------------------
    pass_8 = os.environ.get("ANTHROPIC_API_KEY") is None
    assertion(
        8,
        "Zero LLM invariant: ANTHROPIC_API_KEY absent throughout canary execution",
        pass_8,
        observed={"anthropic_api_key_present": os.environ.get("ANTHROPIC_API_KEY") is not None},
        expected={"anthropic_api_key_present": False},
    )

    # ----------------------------------------------------------------
    # A9 (Cond 1): zero plaintext orphans in the table
    # ----------------------------------------------------------------
    # Try decrypt every row. Any row that fails = plaintext orphan =
    # back-fill not run. This makes the "I forgot to run the script"
    # failure mode a CI red.
    env_fernet = Fernet(os.environ["TEAM_PHONE_ENCRYPTION_KEY"].encode())
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT phone_token, phone_number_encrypted "
            "FROM phone_token_resolutions "
            "WHERE phone_number_encrypted IS NOT NULL"
        )
        all_rows = cur.fetchall()
    plaintext_orphans: list[str] = []
    for r in all_rows:
        try:
            env_fernet.decrypt(r["phone_number_encrypted"].encode())
        except InvalidToken:
            plaintext_orphans.append(r["phone_token"])
    pass_9 = len(plaintext_orphans) == 0 and len(all_rows) > 0
    assertion(
        9,
        "ZERO plaintext orphans: every phone_token_resolutions row decrypts under env key (Cond 1)",
        pass_9,
        observed={
            "total_rows": len(all_rows),
            "plaintext_orphans_count": len(plaintext_orphans),
            "plaintext_orphans_sample": plaintext_orphans[:3],
        },
        expected={
            "plaintext_orphans_count": 0,
            "total_rows_gt": 0,
        },
    )

    return _finalise(pool)


def _finalise(pool) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print("\n=== Anthropic cost: 0 paise (deterministic crypto substrate; no LLM) ===")

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM privacy_audit_log "
                "WHERE payload->>'phone_token' = ANY(%s)",
                (INSERTED_TOKEN_IDS,),
            )
            cur.execute(
                "DELETE FROM phone_token_resolutions "
                "WHERE phone_token = ANY(%s)",
                (INSERTED_TOKEN_IDS,),
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
