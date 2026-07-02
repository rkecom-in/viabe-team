"""VT-531 (C3) + VT-561 — the reviewer-correction store, now a TRAINABLE-PAIR store.

Capture every owner edit/reject/approve verdict the moment it lands in ``apply_agent_decision`` —
BEFORE ``redact_batch_close`` sha256s the raw ``owner_feedback`` AND the ``agent_drafts`` params
into oblivion. Each row is a self-contained (proposal → verdict → correction) example:

- ``correction_text``   — the owner's PII-REDACTED prose (the verbal correction), NOT sha256'd.
- ``proposal_snapshot`` — a PII-REDACTED snapshot of what the agent PROPOSED (per-draft template +
  params). VT-561 (finding a): without this the label survived but the artifact it labels was
  sha256-destroyed by redact_batch_close in the SAME txn — a label with no example is not trainable.
- ``correction_kind='approve'`` — VT-561 (finding b): approve-as-is now writes a labeled POSITIVE
  example (proposal_snapshot, no correction_text) so the dataset is not all-negatives.

Redaction posture (binding): every snapshot goes through the SAME ``pii_redactor.redact`` that owns
``correction_text`` — NOT the sha256 ``outbox_redaction`` destroy. The learning SUBSTANCE (template +
redacted params) survives; customer PII is stripped. Redaction happens HERE (inside
``record_correction``, the same layer ``correction_text`` is redacted at) so no caller can forget.

Append-only; capture-now, retrieve-later (the gate columns default closed).

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

# VT-561 snapshot cap. The always-confirm floor forces owner approval for any batch larger than
# L3_AUTO_MAX_BATCH (=20, autonomy.is_always_confirm 'bulk'), so an approval-gated batch is normally
# <= 20 drafts; this cap bounds the pathological case. A batch's drafts share one template and differ
# only in per-customer params, so the first N proposals are a faithful example — the TOTAL count + a
# truncation flag are recorded either way.
_SNAPSHOT_MAX_DRAFTS = 20


def load_batch_draft_snapshot(
    conn: Any,
    tenant_id: UUID | str,
    batch_id: UUID | str,
    *,
    limit: int = _SNAPSHOT_MAX_DRAFTS,
) -> dict[str, Any] | None:
    """Read the batch's still-'drafted' rows (what the owner was judging) on the caller's conn and
    return a RAW proposal snapshot — ``{"drafts": [{"template_name", "params"}], "draft_count",
    "captured", "truncated"}`` — or None when the batch has no drafted rows.

    RAW by design: the redaction is ``record_correction``'s job (the ``correction_text`` layer).
    Filters status='drafted' to match the arm-time sample
    (``approval_glue._render_sample_message`` / ``_batch_draft_count``); at every call site (reject /
    edit / approve) the proposal rows are still 'drafted' — ``redact_batch_close`` has NOT yet run,
    so the params are raw. Capped at ``limit`` drafts (ordered by created_at, the send-preview order)
    with the total count + a truncation flag preserved.

    FAIL-SOFT like ``record_correction`` itself (the snapshot is capture, and capture must never
    break the approval resolution): the reads run inside their OWN nested transaction (⇒ SAVEPOINT
    when the caller is mid-txn), so a read failure rolls back only the savepoint — the caller's
    resolution txn stays usable — and returns None (lesson lost, WARNING logged, owner unharmed).
    Argument-position matters: this is evaluated BEFORE ``record_correction``'s own try at every
    call site, so without this envelope a read error would unwind the whole resolution txn and
    discard the owner's approve/reject reply.
    """
    tid, bid = str(tenant_id), str(batch_id)
    try:
        with conn.transaction():  # nested ⇒ SAVEPOINT: a failed read must not poison the caller's txn
            total_row = conn.execute(
                "SELECT count(*) AS n FROM agent_drafts "
                "WHERE tenant_id = %s AND batch_id = %s AND status = 'drafted'",
                (tid, bid),
            ).fetchone()
            total = int((total_row["n"] if isinstance(total_row, dict) else total_row[0]) or 0)
            if total == 0:
                return None
            rows = conn.execute(
                "SELECT template_name, params FROM agent_drafts "
                "WHERE tenant_id = %s AND batch_id = %s AND status = 'drafted' "
                "ORDER BY created_at ASC LIMIT %s",
                (tid, bid, int(limit)),
            ).fetchall()
    except Exception:  # noqa: BLE001 — capture is fail-soft; the approval resolution must not break
        logger.warning(
            "load_batch_draft_snapshot failed (fail-soft; lesson lost) batch=%s", bid, exc_info=True
        )
        return None
    drafts = [
        {
            "template_name": (r["template_name"] if isinstance(r, dict) else r[0]),
            "params": (r["params"] if isinstance(r, dict) else r[1]) or {},
        }
        for r in rows
    ]
    return {
        "drafts": drafts,
        "draft_count": total,
        "captured": len(drafts),
        "truncated": total > len(drafts),
    }


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
    proposal_snapshot: dict[str, Any] | None = None,
    corrected_snapshot: dict[str, Any] | None = None,
) -> None:
    """Append one correction row on the caller's conn (SAVEPOINT-isolated, fail-soft).

    ``correction_kind`` is a RegressionKind value plus VT-561's ``approve`` (the positive lesson);
    ``owner_feedback`` is PII-redacted (never sha256'd) before insert so the substance is retrievable
    later. ``proposal_snapshot`` / ``corrected_snapshot`` are RAW structured snapshots (see
    ``load_batch_draft_snapshot``) — redacted HERE through the SAME pii_redactor, then stored JSONB,
    so a caller can never persist an un-redacted draft.
    """
    try:
        with conn.transaction():  # nested ⇒ SAVEPOINT (caller in a txn) / a txn of its own (autocommit)
            from psycopg.types.json import Jsonb  # lazy: keep module import dep-less

            proposal = _redact_snapshot(proposal_snapshot)
            corrected = _redact_snapshot(corrected_snapshot)
            conn.execute(
                "INSERT INTO agent_corrections "
                "(tenant_id, run_id, batch_id, agent, correction_kind, decision_verb, "
                " correction_text, proposal_snapshot, corrected_snapshot) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(tenant_id),
                    str(run_id) if run_id is not None else None,
                    str(batch_id) if batch_id is not None else None,
                    agent,
                    correction_kind,
                    decision_verb,
                    _redact_text(owner_feedback) if owner_feedback else None,
                    Jsonb(proposal) if proposal is not None else None,
                    Jsonb(corrected) if corrected is not None else None,
                ),
            )
    except Exception:  # noqa: BLE001 — capture is fail-soft; the approval resolution must not break
        logger.warning("VT-531 correction capture failed (fail-soft)", exc_info=True)


def _redact_text(text: str) -> str:
    out = redact(text)
    return out if isinstance(out, str) else str(out)


def _redact_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """PII-redact a raw proposal/corrected snapshot through the canonical ``pii_redactor.redact``
    (the SAME redactor + posture as ``correction_text`` — substance kept, PII stripped, NOT sha256).

    ``redact`` is a structure-preserving walker: dict KEYS survive (so ``template_name`` and the
    param slot names stay readable), values at PII keys (customer_name / phone / …) tokenize, and
    every other string leaf gets pattern-driven redaction (phone / email / PAN / …). A
    ``template_name`` value ('team_winback_simple') matches no PII pattern and passes through intact
    — the load-bearing training signal is kept. The snapshot's flat shape stays within the redactor's
    depth budget (drafts → draft → params → value). Returns None for None (nothing to store)."""
    if snapshot is None:
        return None
    out = redact(snapshot)
    return out if isinstance(out, dict) else {"redacted": out}


__all__ = ["load_batch_draft_snapshot", "record_correction"]
