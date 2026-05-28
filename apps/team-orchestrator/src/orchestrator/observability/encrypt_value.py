"""VT-207 generic Fernet wrapper.

Extracted from VT-191's ``phone_tokens.py`` per Cowork Q2 lock. Both
phone encryption AND OAuth token encryption now use the same shared
Fernet helper. Key remains ``TEAM_PHONE_ENCRYPTION_KEY`` (rename
deferred — semantic separation of the key per data class is a future
concern; today both classes are protected by the same secret).

Per CL-390: any plaintext-at-rest material crosses through this seam.
Per CL-71: callers carry tenant scoping; this module is pure crypto.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


_FERNET_KEY_ENV = "TEAM_PHONE_ENCRYPTION_KEY"


def _fernet() -> Fernet:
    key = os.environ.get(_FERNET_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(
            f"{_FERNET_KEY_ENV} not set "
            "(generate via: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
        )
    return Fernet(key.encode())


def encrypt_value(plaintext: str) -> str:
    """Fernet-encrypt a UTF-8 plaintext string. Returns base64-URL-safe ciphertext."""
    if not isinstance(plaintext, str):
        raise TypeError(f"encrypt_value: expected str, got {type(plaintext).__name__}")
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Fernet-decrypt. Raises ``cryptography.fernet.InvalidToken`` on bad input."""
    if not isinstance(ciphertext, str):
        raise TypeError(f"decrypt_value: expected str, got {type(ciphertext).__name__}")
    return _fernet().decrypt(ciphertext.encode()).decode()


__all__ = ["encrypt_value", "decrypt_value", "InvalidToken"]
