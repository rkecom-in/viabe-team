#!/usr/bin/env python3
"""VT-191 back-fill — encrypt existing plaintext rows in phone_token_resolutions.

VT-184 Phase-1 stored phone_e164 as plaintext in the
``phone_number_encrypted`` column with a 3-layer ⚠️ WARNING. Migration 028
updates the column COMMENT (schema metadata); this script transforms
the data (Q2 Option A1 — Cowork plan-review 2026-05-27 locked
separation of concerns).

Idempotent via try-decrypt-then-encrypt-on-fail:
  - For each row, try ``Fernet(KEY).decrypt(phone_number_encrypted)``.
  - On success → row already ciphertext, skip (count `already_encrypted`).
  - On ``InvalidToken`` → treat as plaintext, encrypt + UPDATE
    (count `newly_encrypted`).

Usage::

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      ./.venv/bin/python ../../scripts/vt191_encrypt_existing_rows.py
    )

Requires:
  - ``DATABASE_URL`` env (postgres DSN)
  - ``TEAM_PHONE_ENCRYPTION_KEY`` env (Fernet key)

Output: JSON line with ``{already_encrypted, newly_encrypted, total}``
to stdout, suitable for ops-script consumption.

Re-running on a fully-encrypted table is a no-op (``newly_encrypted = 0``).
VT-191 canary assertion 7 verifies this idempotency.
"""

from __future__ import annotations

import json
import os
import sys

import psycopg
from cryptography.fernet import Fernet, InvalidToken


def main() -> int:
    key = os.environ.get("TEAM_PHONE_ENCRYPTION_KEY", "")
    if not key:
        print(
            "PREFLIGHT FAIL — TEAM_PHONE_ENCRYPTION_KEY not set. Generate via:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"',
            file=sys.stderr,
        )
        return 2

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("PREFLIGHT FAIL — DATABASE_URL not set", file=sys.stderr)
        return 2

    fernet = Fernet(key.encode() if isinstance(key, str) else key)
    already_encrypted = 0
    newly_encrypted = 0

    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT phone_token, phone_number_encrypted "
                    "FROM phone_token_resolutions "
                    "WHERE phone_number_encrypted IS NOT NULL"
                )
                rows = cur.fetchall()
                for token, current in rows:
                    try:
                        fernet.decrypt(current.encode())
                        already_encrypted += 1
                        continue
                    except InvalidToken:
                        pass
                    ciphertext = fernet.encrypt(current.encode()).decode()
                    cur.execute(
                        "UPDATE phone_token_resolutions "
                        "SET phone_number_encrypted = %s "
                        "WHERE phone_token = %s",
                        (ciphertext, token),
                    )
                    newly_encrypted += 1

    print(
        json.dumps(
            {
                "already_encrypted": already_encrypted,
                "newly_encrypted": newly_encrypted,
                "total": already_encrypted + newly_encrypted,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
