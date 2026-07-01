"""VT-531 (C3) — the reviewer-correction store.

Capture every owner edit/reject correction the moment it lands in ``apply_agent_decision`` —
BEFORE ``redact_batch_close`` sha256s the raw ``owner_feedback`` into oblivion. The correction
text is PII-REDACTED (pii_redactor), NOT sha256'd, so the learning SUBSTANCE survives while
customer PII is stripped. Append-only; capture-now, retrieve-later (the gate columns default
closed).

The write runs on the caller's resolution connection inside a SAVEPOINT so it is atomic with the
batch-state transition WHEN it succeeds, yet a capture failure rolls back ONLY the savepoint and is
swallowed — the Pillar-7 approval resolution must never break on an observability write.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from orchestrator.privacy.pii_redactor import redact

logger = logging.getLogger(__name__)


def record_correction(
    conn: Any,
    tenant_id: UUID | str,
    *,
    agent: str | None,
    correction_kind: str,
    decision_verb: str,
    owner_feedback: str | None = None,
    run_id: UUID | str | None = None,
    batch_id: UUID | str | None = None,
) -> None:
    """Append one correction row on the caller's conn (SAVEPOINT-isolated, fail-soft).

    ``correction_kind`` is a RegressionKind value (``edit``/``reject``); ``owner_feedback`` is
    PII-redacted (never sha256'd) before insert so the substance is retrievable later.
    """
    try:
        with conn.transaction():  # nested ⇒ SAVEPOINT (caller in a txn) / a txn of its own (autocommit)
            conn.execute(
                "INSERT INTO agent_corrections "
                "(tenant_id, run_id, batch_id, agent, correction_kind, decision_verb, correction_text) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    str(tenant_id),
                    str(run_id) if run_id is not None else None,
                    str(batch_id) if batch_id is not None else None,
                    agent,
                    correction_kind,
                    decision_verb,
                    _redact_text(owner_feedback) if owner_feedback else None,
                ),
            )
    except Exception:  # noqa: BLE001 — capture is fail-soft; the approval resolution must not break
        logger.warning("VT-531 correction capture failed (fail-soft)", exc_info=True)


def _redact_text(text: str) -> str:
    out = redact(text)
    return out if isinstance(out, str) else str(out)


__all__ = ["record_correction"]
