"""VT-514 — Team-Manager AUDIT / TRACE log.

Single entry point: :func:`emit_tm_audit`. One row in ``tm_audit_log`` per
thing the Team-Manager (or a specialist lane) KNOWS / GETS / DECIDES / DOES /
ASKS. This is the audit *spine* — it REFERENCES the rich substrate rather than
duplicating it:

- reasoning DEPTH stays in ``pipeline_steps`` (agent_reasoning_step); a row
  carries ``reasoning_ref = {"run_id", "step_seq"|"step_id"}`` instead of a
  copy of ``think_text``.
- failures stay in ``debug_events``; a row shares ``trace_id`` so the VT-516
  viewer joins a failure to the exact decision/knowledge it broke on.

Two emit modes — the crux of the VT-514 no-orphan design
--------------------------------------------------------
**conn passed (ACTION layer — FAIL-CLOSED).** The INSERT runs inside the
caller's *existing* transaction (the same txn that commits the draft / approval
/ memory / autonomy / business-action row). It MUST raise on failure so the
caller's transaction rolls back — a DB-transactional side-effect cannot commit
without its audit row. This is the deliberate VT-460 rails analog
(can't-audit ⇒ can't-act), proven by
``tests/agent/test_tm_audit_nonbypassability.py``. Runs as ``app_role`` under
RLS ``tm_audit_app_insert`` (``tenant_id = app_current_tenant()``).

**conn=None (DECISION / KNOWLEDGE / SPAWN / best-effort — FAIL-SOFT).** Opens
its own ``get_pool()`` service-role connection and NEVER raises (byte-for-byte
the :func:`orchestrator.observability.debug_log.emit_debug_event` contract).
Used where there is no business transaction to bind to (reasoning turns, route
decisions, spawn ``Command``, intent classification): complete-by-construction
at a single choke, but not atomically bound — losing one degrades replay, it is
not an un-audited side-effect.

PII by reference (CL-390)
-------------------------
Every free-text / JSONB field is passed through
:func:`orchestrator.privacy.pii_redactor.redact` with the tenant
``name_registry`` BEFORE the INSERT. Phones become ``phone_tok_…`` tokens;
GSTIN/PAN/Aadhaar/email/IFSC/CC pattern-redacted; known customer names
tokenised via the tenant registry (fail-soft to pattern-only, VT-379). No raw
PII at rest; the VTR viewer receives only ids + structured facts + tokens.
Registry build is ALWAYS fail-soft — even in fail-closed mode only the INSERT
itself is fail-closed, never the redaction setup.

The emit generates the row ``id`` client-side (no ``RETURNING``) so the
``app_role`` path never needs SELECT-under-RLS.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

if TYPE_CHECKING:  # pragma: no cover
    import psycopg

logger = logging.getLogger(__name__)

# event_layer vocabulary (advisory — the table has no CHECK so the taxonomy can
# evolve without a migration; a stray value logs a breadcrumb but never blocks).
_LAYERS = {"knows", "gets", "decides", "does", "asks"}

_SUMMARY_MAX = 2000


def emit_tm_audit(
    *,
    event_layer: str,
    event_kind: str,
    actor: str,
    tenant_id: UUID | str,
    run_id: UUID | str | None = None,
    trace_id: str | None = None,
    snapshot_id: str | None = None,
    summary: str | None = None,
    input: dict[str, Any] | None = None,  # noqa: A002 — mirrors the column name
    decision: dict[str, Any] | None = None,
    reasoning_ref: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    severity: str = "info",
    status: str = "ok",
    parent_audit_id: UUID | str | None = None,
    conn: psycopg.Connection | None = None,
) -> UUID | None:
    """Insert one ``tm_audit_log`` row. Returns the row id (or ``None`` on a
    swallowed fail-soft error).

    Parameters
    ----------
    event_layer:
        ``knows`` / ``gets`` / ``decides`` / ``does`` / ``asks``.
    event_kind:
        Specific kind, e.g. ``context_assembled``, ``route_decided``,
        ``draft_created``, ``send_result``, ``approval_armed``,
        ``memory_write``, ``autonomy_change``, ``escalation``, ``ask_owner``,
        ``recovery_attempted`` (VT-530 — a tool error surfaced to the manager
        to recover or terminate; advisory vocab, no CHECK, no migration),
        ``manager_decision`` (VT-526 B3-wiring — the manager decision loop ran
        on a real specialist return; ``status='observed'`` = observe-only, not
        yet steering routing), ``policy_shadow`` (OC1/VT-533 — the customer-send
        policy rail would BLOCK, recorded observe-only while ``enforce_policy`` is
        off; the data that de-risks flipping enforcement on).
    actor:
        ``team_manager`` or the specific lane (``sales_recovery`` / …).
    tenant_id:
        Owning tenant (NOT NULL on the table). In the ``conn``-passed path the
        caller's ``tenant_connection`` must already be scoped to this tenant
        (RLS ``tenant_id = app_current_tenant()``).
    run_id / trace_id / snapshot_id:
        Correlation keys. ``trace_id`` joins ``debug_events``; ``reasoning_ref``
        + ``snapshot_id`` make a decision replayable.
    conn:
        When provided, the INSERT runs in the caller's transaction and RAISES on
        failure (fail-closed action audit). When ``None``, a fail-soft
        service-role write that never raises.

    Notes
    -----
    Honest limit: only what the model *emits* (verbalised reasoning, tool calls)
    and the assembled *input* context are captured; the model's internal latent
    reasoning is not observable. ``snapshot_id`` gives input-replayability, not
    decision-determinism.
    """
    audit_id = uuid4()
    if event_layer not in _LAYERS:
        _warn(f"unknown event_layer={event_layer!r} (kind={event_kind!r}) — inserting anyway")

    # VT-690 phase 2 — mirror this decision-audit event onto the live OTel span (best-effort,
    # independent of the DB write below, so the trace captures the reasoning even if the audit
    # DB is unavailable). This is what makes "why did the agent decide X" visible in Honeycomb
    # alongside the LLM prompt/response + workflow spans.
    _mirror_to_span(
        event_layer=event_layer, event_kind=event_kind, actor=actor, run_id=run_id,
        severity=severity, status=status, tenant_id=tenant_id, summary=summary, input=input,
        decision=decision, reasoning_ref=reasoning_ref, action=action, result=result,
    )

    def _prep() -> tuple[Any, ...]:
        # Redaction + param-build can raise (registry build is itself fail-soft,
        # but redact()/Jsonb import are not). Kept INSIDE the caller's failure
        # boundary in both modes — see the two call sites below.
        return _build_params(
            audit_id=audit_id,
            tenant_id=tenant_id,
            run_id=run_id,
            trace_id=trace_id,
            snapshot_id=snapshot_id,
            event_layer=event_layer,
            event_kind=event_kind,
            actor=actor,
            severity=severity,
            status=status,
            parent_audit_id=parent_audit_id,
            redacted=_redact_fields(
                tenant_id=tenant_id,
                summary=summary,
                input=input,
                decision=decision,
                reasoning_ref=reasoning_ref,
                action=action,
                result=result,
            ),
        )

    if conn is not None:
        # FAIL-CLOSED: caller owns the transaction; redaction, param-build AND the
        # insert may all raise → the caller's transaction rolls back
        # (can't-audit ⇒ can't-act, the VT-460 rails analog).
        _execute(conn, _prep())
        return audit_id

    # FAIL-SOFT: EVERYTHING (redact, param-build, connect, insert) is wrapped —
    # this path NEVER propagates. Load-bearing for the send-result choke: a raise
    # here would surface AFTER the external WhatsApp send + committed draft flip,
    # risking a double-send on caller retry.
    try:
        params = _prep()
        from orchestrator.graph import get_pool

        with get_pool().connection() as own:
            _execute(own, params)
        return audit_id
    except BaseException as exc:  # noqa: BLE001 — must never propagate (conn=None contract)
        _warn(
            f"tm_audit_log insert failed (fail-soft): "
            f"layer={event_layer!r} kind={event_kind!r} actor={actor!r} "
            f"exc={type(exc).__name__}: {exc}"
        )
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_INSERT_SQL = """
    INSERT INTO tm_audit_log
        (id, tenant_id, run_id, trace_id, snapshot_id,
         event_layer, event_kind, actor, summary,
         input, decision, reasoning_ref, action, result,
         severity, status, parent_audit_id)
    VALUES
        (%s, %s, %s, %s, %s,
         %s, %s, %s, %s,
         %s, %s, %s, %s, %s,
         %s, %s, %s)
"""


def _redact_fields(
    *,
    tenant_id: UUID | str,
    summary: str | None,
    input: dict[str, Any] | None,
    decision: dict[str, Any] | None,
    reasoning_ref: dict[str, Any] | None,
    action: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    """Redact every free-text / JSONB field. Registry build is fail-soft."""
    from orchestrator.privacy.pii_redactor import redact

    registry = _registry_for_tenant(tenant_id)

    def _r_text(v: str | None) -> str | None:
        if not v:
            return None
        out = redact(v, name_registry=registry)
        return str(out)[:_SUMMARY_MAX] if out else None

    def _r_obj(v: dict[str, Any] | None) -> dict[str, Any] | None:
        if not v:
            return None
        out = redact(v, name_registry=registry)
        return out if isinstance(out, dict) else {"_raw": str(out)}

    return {
        "summary": _r_text(summary),
        # reasoning_ref is a structured pointer ({run_id, step_seq}); redact
        # defensively in case a caller stuffs free text into it.
        "reasoning_ref": _r_obj(reasoning_ref),
        "input": _r_obj(input),
        "decision": _r_obj(decision),
        "action": _r_obj(action),
        "result": _r_obj(result),
    }


# VT-690 phase 2 — cap each JSON-encoded field so a large assembled context can't bloat a span.
_SPAN_ATTR_MAX = 4096


def _mirror_to_span(
    *,
    event_layer: str,
    event_kind: str,
    actor: str,
    run_id: UUID | str | None,
    severity: str,
    status: str,
    tenant_id: UUID | str,
    summary: str | None,
    input: dict[str, Any] | None,  # noqa: A002 — mirrors emit_tm_audit's param name
    decision: dict[str, Any] | None,
    reasoning_ref: dict[str, Any] | None,
    action: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> None:
    """VT-690 phase 2 — mirror a decision-audit event onto the CURRENT OTel span as a span event,
    so the GETS->KNOWS->DECIDES->DOES progression (incl. ``reasoning_ref`` = the WHY) rides the
    Honeycomb trace alongside the LLM prompt/response (``instrument_anthropic``) + DBOS workflow
    spans, correlated by ``run_id``. PII passes through the SAME ``_redact_fields`` the DB write
    uses. NEVER raises (a parallel observability sink must never affect the audit path). No-op when
    tracing is off (no ``HONEYCOMB_API_KEY``) or no span is currently recording.
    """
    try:
        from orchestrator.observability.logfire import is_enabled

        if not is_enabled():
            return
        from opentelemetry import trace as _otel_trace

        span = _otel_trace.get_current_span()
        if span is None or not span.is_recording():
            return
        redacted = _redact_fields(
            tenant_id=tenant_id, summary=summary, input=input, decision=decision,
            reasoning_ref=reasoning_ref, action=action, result=result,
        )
        attrs: dict[str, Any] = {
            "tm_audit.layer": event_layer,
            "tm_audit.kind": event_kind,
            "tm_audit.actor": actor,
            "tm_audit.severity": severity,
            "tm_audit.status": status,
        }
        if run_id is not None:
            attrs["run_id"] = str(run_id)
        for field in ("summary", "reasoning_ref", "input", "decision", "action", "result"):
            v = redacted.get(field)
            if v is None:
                continue
            s = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
            attrs[f"tm_audit.{field}"] = s[:_SPAN_ATTR_MAX]
        span.add_event(f"tm_audit.{event_layer}.{event_kind}", attributes=attrs)
    except Exception:  # noqa: BLE001 — parallel observability sink; never touch the audit write
        pass


def _build_params(
    *,
    audit_id: UUID,
    tenant_id: UUID | str,
    run_id: UUID | str | None,
    trace_id: str | None,
    snapshot_id: str | None,
    event_layer: str,
    event_kind: str,
    actor: str,
    severity: str,
    status: str,
    parent_audit_id: UUID | str | None,
    redacted: dict[str, Any],
) -> tuple[Any, ...]:
    from psycopg.types.json import Jsonb

    def _j(v: dict[str, Any] | None) -> Any:
        return Jsonb(v) if v is not None else None

    return (
        str(audit_id),
        str(tenant_id),
        str(run_id) if run_id is not None else None,
        trace_id,
        snapshot_id,
        event_layer,
        event_kind,
        actor,
        redacted["summary"],
        _j(redacted["input"]),
        _j(redacted["decision"]),
        _j(redacted["reasoning_ref"]),
        _j(redacted["action"]),
        _j(redacted["result"]),
        severity,
        status,
        str(parent_audit_id) if parent_audit_id is not None else None,
    )


def _execute(conn: psycopg.Connection, params: tuple[Any, ...]) -> None:
    """Run the INSERT on the given connection. Raises on failure (the caller's
    fail-closed contract for the conn-passed path; swallowed upstream for
    conn=None)."""
    with conn.cursor() as cur:
        cur.execute(_INSERT_SQL, params)


def _registry_for_tenant(tenant_id: UUID | str):  # type: ignore[no-untyped-def]
    """Build the tenant customer-name registry — fail-soft to pattern-only.

    Mirrors ``pipeline_observability._registry_for_tenant``: a registry-build
    failure (customers read error, pool unavailable) degrades to pattern-only
    redaction and NEVER breaks the emit — even on the fail-closed action path,
    only the INSERT is fail-closed, not the redaction setup.
    """
    try:
        from orchestrator.privacy.customer_registry import make_name_registry

        return make_name_registry(str(tenant_id))
    except Exception:  # noqa: BLE001 — fail-soft by contract
        _warn(f"name-registry build failed for tenant={tenant_id!r}; pattern-only redaction")
        return None


def _warn(msg: str) -> None:
    """One-line stderr + logger breadcrumb. Never raises."""
    try:
        logger.warning("[tm_audit] %s", msg)
        print(f"[observability/tm_audit] {msg}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


__all__ = ["emit_tm_audit"]
