"""VT-608 Package 5, RULING 3 — the deterministic, SERVER-SIDE ingestion-commit executor.

VT-268's fail-closed guardrail stands: the integration agent's own ``commit_ingestion`` tool
(``agent/integration_agent.py``) NEVER writes a customer/ledger row itself — it only PROPOSES
(persists a ``tenant_integration_state.pending_owner_input`` envelope with
``awaiting='ingestion_commit_pending'``). This module is the non-agent, deterministic code path
that actually performs the write, mirroring the campaign effect rail's shape (propose in the
specialist -> execute server-side, deterministically, once the proposal is accepted) without
introducing a NEW owner-facing approval gate — an owner importing their OWN already-OAuth'd data
source is not a customer-facing send, so (unlike a campaign) there is nothing here for the owner
to approve; "accepted" is the specialist's own turn concluding with the proposal intact.

Two call sites invoke ``execute_pending_ingestion_commit`` (both deterministic, non-agent):
  - ``runner.py``, right after ``dispatch_brain`` returns (the LIVE legacy/shadow path today).
  - ``manager/workflow.py``'s ``_dispatch_specialist_step``, right after ``graph.invoke`` returns,
    when the just-dispatched step targeted ``integration_agent`` (the enforce-mode loop path).
Both call the SAME function against the SAME ``tenant_integration_state`` truth — no dual-writer
race (mirrors RULING 1's "both paths write through the same phase-state functions").

Reuses PROVEN mappers rather than re-deriving a mapping-driven row transform: Shopify's own
``pull_and_ingest_shopify`` (identical to the existing deterministic flow) and Sheets'
``ingest.sheet_row_to_canonical`` (the SAME alias-based mapper ``integrations/scheduler.py``
already uses for its own google_sheet ingestion). The propose/confirm mapping REASONER
(``integrations/field_mapping.py``) drives owner-facing transparency + the ask-owner threshold
routing, not the row transform itself — see ``agent/integration_agent.py``'s ``propose_mapping`` /
``confirm_mapping`` docstrings for the full rationale.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from orchestrator.db import tenant_connection

logger = logging.getLogger(__name__)

_AWAITING_COMMIT_PENDING = "ingestion_commit_pending"
_DEFAULT_CADENCE = "0 3 * * *"  # daily 03:00 IST — mirrors shopify_onboarding._schedule_recurring


def is_connector_connected(tenant_id: UUID | str, connector_id: str) -> bool:
    """True iff a ``tenant_oauth_tokens`` row exists for ``(tenant, connector)`` — the durable,
    DB-truth signal an OAuth install completed. Generalizes
    ``shopify_onboarding.shopify_is_connected`` (hardcoded to ``connector_id='shopify'``) to any
    connector; the ``check_oauth_status`` agent tool is this function's caller."""
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT 1 FROM tenant_oauth_tokens WHERE tenant_id = %s AND connector_id = %s LIMIT 1",
            (str(tenant_id), connector_id),
        ).fetchone()
    return row is not None


def _schedule_default_cadence(tenant_id: UUID | str, connector_id: str) -> None:
    """Best-effort: mirrors ``shopify_onboarding._schedule_recurring`` exactly (same default daily
    cadence), generalized to any connector. A scheduling failure must not block the commit that
    already landed — the owner's data is safely in; a missed cadence write just means the next
    ``schedule_recurring_pull`` tool call (or a future retry) sets it, never data loss."""
    try:
        from datetime import UTC, datetime

        from orchestrator.integrations.scheduler import _compute_next_run

        next_run = _compute_next_run(_DEFAULT_CADENCE, datetime.now(UTC))
        with tenant_connection(tenant_id) as conn:
            conn.execute(
                """
                INSERT INTO tenant_connector_status (
                    tenant_id, connector_id, pull_cadence, next_scheduled_run, enabled
                ) VALUES (%s, %s, %s, %s, TRUE)
                ON CONFLICT (tenant_id, connector_id) DO UPDATE SET
                    pull_cadence = EXCLUDED.pull_cadence,
                    next_scheduled_run = EXCLUDED.next_scheduled_run,
                    enabled = TRUE,
                    updated_at = now()
                """,
                (str(tenant_id), connector_id, _DEFAULT_CADENCE, next_run),
            )
    except Exception:  # noqa: BLE001 — non-blocking; the commit itself already succeeded
        logger.warning(
            "VT-608: default recurring-cadence schedule failed tenant=%s connector=%s (non-blocking)",
            tenant_id, connector_id,
        )


def _commit_shopify(tenant_id: UUID | str) -> dict[str, int]:
    from orchestrator.onboarding.shopify_onboarding import pull_and_ingest_shopify

    return pull_and_ingest_shopify(tenant_id)


def _confirmed_field_mapping(tenant_id: UUID | str, connector_id: str) -> dict[str, str] | None:
    """VT-608 fix round CRITICAL 2 — read the owner-confirmed mapping back from its DURABLE home
    (``tenant_connector_status.field_mapping``, migration 168 — persisted by the
    ``commit_ingestion`` tool at proposal time), never the ephemeral
    ``tenant_integration_state.pending_owner_input`` envelope (which later phase transitions
    overwrite). ``None`` when no mapping was ever confirmed (Shopify never calls
    ``confirm_mapping`` at all — fixed-schema, no reasoner) — callers fall back to the alias
    table, unchanged."""
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT field_mapping FROM tenant_connector_status "
            "WHERE tenant_id = %s AND connector_id = %s",
            (str(tenant_id), connector_id),
        ).fetchone()
    if row is None:
        return None
    mapping = row["field_mapping"] if isinstance(row, dict) else row[0]
    return mapping if isinstance(mapping, dict) else None


def _commit_google_sheet(tenant_id: UUID | str, metadata: dict[str, Any]) -> dict[str, int]:
    from orchestrator.integrations.connectors.google_sheet import GoogleSheetConnector
    from orchestrator.integrations.ingest import CanonicalRow, ingest_customer_rows, sheet_row_to_canonical

    spreadsheet_id = str(metadata.get("spreadsheet_id") or "")
    tab_name = str(metadata.get("tab_name") or "")
    if not spreadsheet_id:
        raise ValueError("_commit_google_sheet: no spreadsheet_id on the ingestion-commit proposal")

    field_mapping = _confirmed_field_mapping(tenant_id, "google_sheet")
    pulled = GoogleSheetConnector().pull_full(
        UUID(str(tenant_id)), spreadsheet_id, tab_name=tab_name
    )
    rows: list[CanonicalRow] = [
        c
        for r in pulled
        if isinstance(r, dict) and (c := sheet_row_to_canonical(r, mapping=field_mapping)) is not None
    ]
    summary = ingest_customer_rows(tenant_id, rows, acquired_via="google_sheet")
    logger.info(
        "VT-608 _commit_google_sheet tenant=%s rows_pulled=%d mapped=%d committed=%d "
        "sales_written=%d (counts only — no PII)",
        tenant_id, len(pulled), len(rows), summary.committed, summary.sales_written,
    )
    return {
        "rows_pulled": len(pulled),
        "mapped": len(rows),
        "committed": summary.committed,
        "ambiguous": summary.ambiguous,
        "dropped": summary.dropped,
        "sales_written": summary.sales_written,
        "sales_skipped_duplicate": summary.sales_skipped_duplicate,
        # VT-608 fix round MINOR 1 — see IngestSummary's own docstring.
        "new_customers": summary.new_customers,
    }


def _revert_stale_proposal(
    tenant_id: UUID | str, connector_id: str, metadata: dict[str, Any], *, reason_code: str
) -> None:
    """VT-608 fix round MAJOR 1 — a proposal that failed the arming-identity or expiry check is
    NEVER executed, but it also must not dangle at ``ingestion_commit_pending`` forever (the
    unconditional executor call would otherwise re-check — and re-skip — it on every future
    turn, silently, forever). Revert to ``field_mapping_confirm`` (the confirmed mapping /
    spreadsheet selection is still valid — only the STALE commit attempt is discarded), carrying
    every OTHER metadata key forward so a fresh ``commit_ingestion`` call can retry cleanly."""
    from orchestrator.onboarding.shopify_onboarding import (
        PHASE_MAPPING,
        _validated_pending,
        _write_state,
    )

    clean_metadata = {k: v for k, v in metadata.items() if k not in ("armed_turn_id", "armed_at")}
    pending = _validated_pending(
        awaiting="field_mapping_confirm",
        prompt_text="Ready to try committing again.",
        connector_id=connector_id,
        metadata=clean_metadata,
    )
    _write_state(tenant_id, phase=PHASE_MAPPING, connector_id=connector_id, pending=pending)
    logger.warning(
        "VT-608 execute_pending_ingestion_commit: STALE proposal reverted (never executed) "
        "tenant=%s connector=%s reason=%s",
        tenant_id, connector_id, reason_code,
    )


def _send_confirmation(tenant_id: UUID | str, connector_id: str, counts: dict[str, Any]) -> None:
    """VT-608 fix round MINOR 2 — the success confirmation was PERSISTED (into
    ``pending_owner_input.prompt_text``) but never actually SENT to the owner. Mirrors
    ``shopify_onboarding.maybe_resume_shopify_onboarding``'s own ``_send`` pattern at its
    ``phase_5_confirmed`` transition. Looks up ``owner_phone`` itself (mirrors
    ``manager.workflow._maybe_reengage_stale``'s own lookup) — NEITHER call site
    (``runner.py`` / ``manager/workflow.py``) has a ready-to-hand recipient phone for this
    specific hook. Best-effort: a send failure must never fail the commit that already landed."""
    from orchestrator.onboarding.shopify_onboarding import _send

    # MINOR 1 — report the NEW-customer count, not the raw committed count (which also includes
    # rows that matched an EXISTING customer — see IngestSummary's own docstring).
    new_count = counts.get("new_customers", counts.get("committed", 0))
    text = (
        f"Done — I connected {connector_id} and found {new_count} new "
        "customers. I'll keep them up to date and start spotting sales to recover."
    )
    try:
        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT owner_phone FROM tenants WHERE id = %s", (str(tenant_id),)
            ).fetchone()
        owner_phone = (row["owner_phone"] if isinstance(row, dict) else row[0]) if row else None
        _send(owner_phone, text, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — the commit already landed; a send failure is non-blocking
        logger.warning(
            "VT-608: ingestion-commit confirmation send failed tenant=%s connector=%s (non-blocking)",
            tenant_id, connector_id,
        )


def execute_pending_ingestion_commit(
    tenant_id: UUID | str, *, current_turn_id: str
) -> dict[str, Any] | None:
    """The RULING 3 executor. Reads ``tenant_integration_state``; a no-op (returns ``None``)
    unless the tenant's pending envelope is EXACTLY an ``ingestion_commit_pending`` proposal — safe
    to call unconditionally after every dispatch (both call sites do). On a genuine, SAME-TURN
    proposal: performs the real commit (connector-dispatched), auto-schedules the default daily
    cadence (mirrors Shopify's own existing behavior — ``schedule_recurring_pull`` remains
    available for the owner to override afterward), advances ``phase`` to ``phase_5_confirmed``
    with ``awaiting='cadence_choice'`` (the SAME terminal shape ``maybe_resume_shopify_onboarding``
    already produces), SENDS the owner a confirmation (MINOR 2), and returns the connector's
    count-only summary (CL-390 — no PII).

    ``current_turn_id`` (VT-608 fix round MAJOR 1) — the CALLER's own turn identity (the webhook
    ``run_id`` in ``runner.py``'s legacy/shadow path; ``task_id`` in ``manager/workflow.py``'s
    enforce-mode path — both match what ``commit_ingestion`` captured via the SAME
    ``ObservabilityContext.run_id`` at proposal time). A proposal whose ``armed_turn_id`` does NOT
    match — or one that has simply expired (``_pending_is_unexpired``, belt-and-braces) — is NEVER
    executed: a failed/interrupted commit from an EARLIER turn must not silently re-fire a FULL
    ingest against whatever unrelated turn happens to poll next, days later. It is instead
    reverted to an honest, retryable ``field_mapping_confirm`` state (see
    ``_revert_stale_proposal``).

    A commit FAILURE (connector/API error) is NOT silently swallowed into the pending envelope —
    the phase does not advance, so the tenant stays observably ``ingestion_commit_pending``
    (visible to ``verify_connector`` as "commit not yet confirmed"); the exception is logged and
    re-raised is deliberately NOT done here (this runs on the hot inbound/dispatch path in both
    call sites) — instead a structured failure marker is written so a human/ops surface can see it,
    never a fabricated success. A SUBSEQUENT call for this same stale proposal will fail the
    arming-identity check above (the next turn's identity differs) and revert honestly rather than
    retry the same failed commit forever.
    """
    from orchestrator.onboarding.shopify_onboarding import (
        PHASE_CONFIRMED,
        _pending_is_unexpired,
        _validated_pending,
        _write_state,
        read_integration_state,
    )

    state = read_integration_state(tenant_id)
    if state is None:
        return None
    pending = state.get("pending_owner_input")
    if not isinstance(pending, dict) or pending.get("awaiting") != _AWAITING_COMMIT_PENDING:
        return None

    connector_id = str(pending.get("connector_id") or state.get("current_connector_id") or "")
    metadata = pending.get("metadata") or {}

    if not _pending_is_unexpired(pending):
        _revert_stale_proposal(tenant_id, connector_id, metadata, reason_code="expired")
        return {"status": "stale_skipped", "connector_id": connector_id, "reason_code": "expired"}

    armed_turn_id = metadata.get("armed_turn_id")
    if armed_turn_id is None or armed_turn_id != str(current_turn_id):
        _revert_stale_proposal(tenant_id, connector_id, metadata, reason_code="arming_mismatch")
        return {
            "status": "stale_skipped", "connector_id": connector_id,
            "reason_code": "arming_mismatch",
        }

    try:
        if connector_id == "shopify":
            counts = _commit_shopify(tenant_id)
        elif connector_id == "google_sheet":
            counts = _commit_google_sheet(tenant_id, metadata)
        else:
            logger.error(
                "VT-608 execute_pending_ingestion_commit: unrecognized connector_id=%r "
                "tenant=%s — leaving phase unchanged (blocked, not silently dropped)",
                connector_id, tenant_id,
            )
            return {"status": "failed", "reason_code": "unsupported_connector"}
    except Exception as exc:  # noqa: BLE001 — hot dispatch path; never crash the caller's turn
        logger.exception(
            "VT-608 execute_pending_ingestion_commit FAILED tenant=%s connector=%s — "
            "phase left at ingestion_commit_pending (never fabricated as confirmed)",
            tenant_id, connector_id,
        )
        return {"status": "failed", "reason_code": type(exc).__name__, "connector_id": connector_id}

    _schedule_default_cadence(tenant_id, connector_id)

    done_pending = _validated_pending(
        awaiting="cadence_choice",
        prompt_text=(
            f"Done — I connected {connector_id} and found "
            f"{counts.get('new_customers', counts.get('committed', 0))} new customers. I'll keep "
            "them up to date and start spotting sales to recover."
        ),
        connector_id=connector_id,
    )
    _write_state(tenant_id, phase=PHASE_CONFIRMED, connector_id=connector_id, pending=done_pending)
    _send_confirmation(tenant_id, connector_id, counts)
    logger.info(
        "VT-608 execute_pending_ingestion_commit CONFIRMED tenant=%s connector=%s committed=%d "
        "(counts only)",
        tenant_id, connector_id, counts.get("committed", 0),
    )
    return {"status": "completed", "connector_id": connector_id, **counts}


__all__ = ["execute_pending_ingestion_commit", "is_connector_connected"]
