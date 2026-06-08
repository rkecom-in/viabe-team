"""VT-113 — canonical From-address resolution (config/email_senders.yaml).

Pillar-8: ONE registry for sender identities; call sites resolve From via ``sender_from(role)`` instead
of hardcoding addresses or reading RESEND_FROM_EMAIL directly. RESEND_FROM_EMAIL, if set, overrides the
'alerts' role for back-compat (it held ops@viabe.ai). Fail-soft: a missing config/role returns "" so
send_resend_email skips (it already guards empty From) — never a crash, never a wrong sender.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG = Path(__file__).resolve().parents[3] / "config" / "email_senders.yaml"


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    try:
        import yaml

        return dict(yaml.safe_load(_CONFIG.read_text()) or {})
    except Exception:
        logger.exception("email_senders: load failed (%s) — fail-soft to empty", _CONFIG)
        return {}


def sender_from(role: str) -> str:
    """The From address for a sending role ('transactional'|'support'|'alerts'). RESEND_FROM_EMAIL
    overrides 'alerts' (back-compat). Returns '' if unresolved (caller skips the send)."""
    if role == "alerts":
        override = os.environ.get("RESEND_FROM_EMAIL", "").strip()
        if override:
            return override
    entry = (_load().get("senders") or {}).get(role) or {}
    addr = str(entry.get("from", "")).strip()
    if not addr:
        logger.warning("email_senders: no From for role=%s", role)
    return addr


def reply_to(role: str) -> str | None:
    entry = (_load().get("senders") or {}).get(role) or {}
    rt = str(entry.get("reply_to", "")).strip()
    return rt or None
