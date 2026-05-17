"""Phone-number tokenisation for orchestrator persistence (VT-3.3a).

TODO VT-122: replace with the full observability writer's phone tokenization.
Keep this implementation simple and deterministic for now.
"""

from __future__ import annotations

import hashlib
import os


def hash_phone(phone_e164: str) -> str:
    """Return a deterministic, salted SHA-256 token for an E.164 phone number.

    Salted with TEAM_PHONE_HASH_SALT so tokens cannot be reversed via a
    rainbow table of known numbers. Same input + same salt -> same token.
    """
    salt = os.environ.get("TEAM_PHONE_HASH_SALT", "")
    if not salt:
        raise RuntimeError(
            "TEAM_PHONE_HASH_SALT not set (generate via: openssl rand -hex 32)"
        )
    digest = hashlib.sha256(f"{salt}:{phone_e164}".encode()).hexdigest()
    return f"phone_tok_{digest}"
