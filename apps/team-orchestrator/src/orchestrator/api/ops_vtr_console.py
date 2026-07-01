"""VT-370 Gap-6 — the VTR console endpoints (plan §6): plan read/edit + agent correction.

Seven POST endpoints under ``/api/orchestrator/ops/``, ALL requiring ``X-Internal-Secret`` +
``X-Operator-Jwt``. Every handler calls :func:`ops_common.require_vtr_action` EXACTLY ONCE before
touching state (the structurally unskippable gate — secret, JWT, body==claim, assignment
fail-closed, denial audited); the returned VERIFIED id is the only value ever used as ``vtr_id`` /
audit attribution.

Read surface (CL-390 PII boundary, DB-enforced): reads go through ``vtr_connection()``
(``SET ROLE app_vtr_role``) against the mig-130 views ONLY — never raw tables. VT-377 (mig-134):
every view read threads the VERIFIED operator id (``vtr_connection(operator_id=...)`` → the
``app.vtr_operator_id`` GUC), so the views themselves scope to the operator's active
operator_assignments — DB-enforced defense in depth BEHIND ``require_vtr_action``'s per-tenant
gate (which stays, as do the single-tenant WHERE clauses). FAZAL break-glass routes to the admin
role (all tenants via the mig-134 role leg). The exception-tier
drill-in (``vtr-batch-drafts``, Fazal=VTR#1) additionally passes ``require_exception_tier`` and
reads via ``SET LOCAL ROLE app_vtr_admin_role`` with the ``draft_params_reveal`` audit row INSERTed
BEFORE the read in the SAME txn (no silent break-glass).

FLAGGED double-leg IDOR defense on ``vtr-plan-edit`` (plan §3 — do NOT drop either leg in a
refactor): leg 1 is ``require_vtr_action`` (operator↔tenant assignment); leg 2 is the seam's
``_locate`` binding ``item_id`` to THAT tenant's latest plan (foreign/stale ids → KeyError → 404).
``item_id`` lives inside ``roadmap_json`` — there is no row to derive a tenant from, so BOTH legs
are load-bearing.

Logging (CL-390): metadata only — operator/tenant/action/counts/status. NEVER ``patch``,
``params``, ``reason``, or any response body. Grounding-violation echoes are ``scrub_pii``'d
BEFORE the HTTPException.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from orchestrator.agents.autonomy import _OPEN_BATCH_STATUSES, cancel_batch, vtr_autonomy_override
from orchestrator.alerts.pii_scrub import scrub_pii
from orchestrator.api.ops_common import (
    audit,
    require_exception_tier,
    require_vtr_action,
    verify_internal_secret,
    verify_operator_jwt,
)
from orchestrator.business_plan import seams
from orchestrator.business_plan.seams import StaleVersion
from orchestrator.business_plan.store import OWNING_AGENTS
from orchestrator.graph import get_pool
from orchestrator.privacy.vtr import vtr_connection

logger = logging.getLogger(__name__)
router = APIRouter()

_DRAFT_BATCH_PAGE_CAP = 200


# ---------------------------------------------------------------------------
# Bodies (plan §6 — exactly as specced)
# ---------------------------------------------------------------------------


class VtrPlanReadBody(BaseModel):
    operator_id: str
    tenant_id: str


class VtrPlanEditBody(BaseModel):
    operator_id: str
    tenant_id: str
    item_id: str
    patch: dict[str, Any]
    expected_prev_version: int


class VtrAgentStateBody(BaseModel):
    operator_id: str
    tenant_id: str


class VtrDraftBatchesBody(BaseModel):
    operator_id: str
    tenant_id: str
    limit: int = 100  # capped at _DRAFT_BATCH_PAGE_CAP


class VtrAutonomyOverrideBody(BaseModel):
    operator_id: str
    tenant_id: str
    agent: str
    action: Literal["freeze", "unfreeze", "demote", "revoke_l3"]
    reason: str = ""


class VtrBatchCancelBody(BaseModel):
    operator_id: str
    batch_id: str
    reason: str = ""
    # NOTE: deliberately NO tenant_id — the tenant is DERIVED from batch_id server-side so a client
    # cannot pair a foreign batch with an assigned tenant (the VT-293/294 IDOR rule).


class VtrBatchDraftsBody(BaseModel):
    operator_id: str
    batch_id: str
    # Exception tier (Fazal=VTR#1) only; tenant derived from batch_id (VT-293/294).


class VtrTenantProfileBody(BaseModel):
    operator_id: str
    tenant_id: str


class VtrConfirmFieldBody(BaseModel):
    operator_id: str
    tenant_id: str
    field: str
    basis: str = ""


class VtrOwnershipDecisionBody(BaseModel):
    operator_id: str
    tenant_id: str
    decision: Literal["verified", "rejected"]
    note: str = ""
    evidence: str = ""


class VtrAgentDirectiveBody(BaseModel):
    operator_id: str
    tenant_id: str
    memory_key: str
    content: str
    agent: str = "manager"
    directive_kind: Literal["strategy", "behavioural"] = "strategy"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_uuid(value: str, field: str) -> str:
    """400 on a non-UUID id before it reaches a uuid-typed SQL param (the run-control idiom)."""
    try:
        UUID(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid {field}") from None
    return value


def _gate(
    *,
    x_internal_secret: str | None,
    x_operator_jwt: str | None,
    body_operator_id: str,
    tenant_id: str,
    deny_action: str,
    deny_target_kind: str = "tenant",
    deny_target_id: str | None = None,
) -> str:
    """Run the shared VTR-action gate on a service-pool cursor (the deny-audit write needs the
    privileged role — app_vtr_role has no ops_audit grant). Returns the VERIFIED operator id."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        return require_vtr_action(
            cur,
            x_internal_secret=x_internal_secret,
            x_operator_jwt=x_operator_jwt,
            body_operator_id=body_operator_id,
            tenant_id=tenant_id,
            deny_action=deny_action,
            deny_target_kind=deny_target_kind,
            deny_target_id=deny_target_id,
        )


def _scrub_reason(reason: str) -> str:
    """Free-text hygiene before any DB write: scrub_pii + clamp 500 (plan §3 — stated honestly,
    scrub_pii catches phones/SIDs/digit-runs only; typed names are kept off VTR read surfaces by
    the view exclusions, not by this scrub)."""
    if not reason:
        return ""
    return scrub_pii(reason)[:500]


def _derive_batch_tenant(conn: Any, batch_id: str) -> dict[str, Any]:
    """Server-side tenant derivation from the batch row (VT-293/294 — never a client tenant_id)."""
    row = conn.execute(
        "SELECT tenant_id, agent FROM agent_draft_batches WHERE id = %s LIMIT 1", (batch_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="batch not found")
    g = dict(row) if isinstance(row, dict) else {"tenant_id": row[0], "agent": row[1]}
    return {"tenant_id": str(g["tenant_id"]), "agent": str(g["agent"])}


# ---------------------------------------------------------------------------
# Screen A — plan read + edit
# ---------------------------------------------------------------------------


@router.post("/api/orchestrator/ops/vtr-plan")
def vtr_plan(
    body: VtrPlanReadBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """Latest plan (vtr_business_plan: latest-version-only, diff-values-stripped) + version-metadata
    history (vtr_plan_history), both read as app_vtr_role — the views are the only door."""
    _require_uuid(body.tenant_id, "tenant_id")
    operator = _gate(
        x_internal_secret=x_internal_secret,
        x_operator_jwt=x_operator_jwt,
        body_operator_id=body.operator_id,
        tenant_id=body.tenant_id,
        deny_action="plan_read_denied",
    )
    with vtr_connection(operator_id=operator) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, version, summary_json, roadmap_json, generated_by, model_id, "
            "delivered_parts, delivered_at, created_at "
            "FROM vtr_business_plan WHERE tenant_id = %s",
            (body.tenant_id,),
        )
        row = cur.fetchone()
        plan = dict(row) if row is not None else None
        cur.execute(
            "SELECT tenant_id, version, generated_by, model_id, created_at "
            "FROM vtr_plan_history WHERE tenant_id = %s ORDER BY version DESC",
            (body.tenant_id,),
        )
        history = [dict(r) for r in cur.fetchall()]
    logger.info(
        "vtr_plan OK operator=%s tenant=%s versions=%d", operator, body.tenant_id, len(history)
    )
    return {"plan": plan, "history": history}


@router.post("/api/orchestrator/ops/vtr-plan-edit")
def vtr_plan_edit(
    body: VtrPlanEditBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """Patch ONE roadmap item via the single Gap-6 seam (re-grounds, appends an immutable version).

    Double-leg IDOR defense (FLAGGED, module docstring): leg 1 = the gate below; leg 2 = the seam's
    ``_locate`` (foreign/stale item_id → KeyError → 404). ``expected_prev_version`` is the optimistic
    concurrency token — replay/race → StaleVersion → 409 (plan-edit is NOT idempotent).
    """
    _require_uuid(body.tenant_id, "tenant_id")
    operator = _gate(
        x_internal_secret=x_internal_secret,
        x_operator_jwt=x_operator_jwt,
        body_operator_id=body.operator_id,
        tenant_id=body.tenant_id,
        deny_action="plan_edit_denied",
    )
    try:
        new_version = seams.edit_roadmap_item(
            body.tenant_id,
            body.item_id,
            body.patch,
            vtr_id=operator,  # the VERIFIED claim id — never the raw body
            expected_prev_version=body.expected_prev_version,
        )
    except StaleVersion as exc:
        # Versions only in the message — safe to echo.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="roadmap item not found in the latest plan"
        ) from exc
    except ValueError as exc:
        # Grounding/patch rejections echo patch tokens — scrub BEFORE the HTTPException (CL-390).
        raise HTTPException(status_code=400, detail=scrub_pii(str(exc))) from exc

    # Operator-attribution index row on the service pool — field NAMES + versions only (CL-390).
    fields = ",".join(sorted(body.patch))
    detail = f"fields=[{fields}] v{body.expected_prev_version}→v{new_version}"
    try:
        pool = get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            audit(
                cur,
                operator_id=operator,
                tenant_id=body.tenant_id,
                action="plan_edit",
                target_kind="roadmap_item",
                target_id=body.item_id,
                detail=detail,
            )
    except Exception as exc:
        # The version stands (append-only; its provenance row still attributes) — fail loud (§4).
        logger.error(
            "vtr_plan_edit audit append FAILED operator=%s tenant=%s item=%s v=%s exc=%r",
            operator, body.tenant_id, body.item_id, new_version, exc,
        )
        raise HTTPException(
            status_code=500,
            detail=f"edit committed as v{new_version} but the audit append failed",
        ) from exc
    logger.info(
        "vtr_plan_edit OK operator=%s tenant=%s item=%s v=%s",
        operator, body.tenant_id, body.item_id, new_version,
    )
    return {"ok": True, "new_version": new_version}


# ---------------------------------------------------------------------------
# Screen B — agent state + correction
# ---------------------------------------------------------------------------


@router.post("/api/orchestrator/ops/vtr-agent-state")
def vtr_agent_state(
    body: VtrAgentStateBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """Per-agent autonomy rows for the tenant — EXACTLY the vtr_agent_autonomy view columns
    (NO revoke_reason — excluded at the view, plan §2). A missing row = L2 default (UI renders)."""
    _require_uuid(body.tenant_id, "tenant_id")
    operator = _gate(
        x_internal_secret=x_internal_secret,
        x_operator_jwt=x_operator_jwt,
        body_operator_id=body.operator_id,
        tenant_id=body.tenant_id,
        deny_action="agent_state_denied",
    )
    with vtr_connection(operator_id=operator) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, tenant_name, agent, level, clean_approval_streak, "
            "lifetime_approvals, lifetime_rejections, frozen, last_regression_at, "
            "last_regression_kind, l3_granted_at, l3_revoked_at, updated_at "
            "FROM vtr_agent_autonomy WHERE tenant_id = %s ORDER BY agent",
            (body.tenant_id,),
        )
        agents = [dict(r) for r in cur.fetchall()]
    logger.info(
        "vtr_agent_state OK operator=%s tenant=%s agents=%d",
        operator, body.tenant_id, len(agents),
    )
    return {"agents": agents}


@router.post("/api/orchestrator/ops/vtr-tenant-profile")
def vtr_tenant_profile(
    body: VtrTenantProfileBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """VT-405 Part A — one tenant's signup fields + auto-discovered draft + keys-only confirmation
    status: EXACTLY the vtr_tenant_profile view columns (non-PII; WhatsApp masked to last-4 AT the
    view, confirmed profile is keys-only). Read-only; the view self-scopes to the operator's active
    assignments via app_vtr_operator() (DB floor under the _gate per-tenant check). A null profile =
    not visible to this operator / no such tenant — the UI renders the empty/denied state."""
    _require_uuid(body.tenant_id, "tenant_id")
    operator = _gate(
        x_internal_secret=x_internal_secret,
        x_operator_jwt=x_operator_jwt,
        body_operator_id=body.operator_id,
        tenant_id=body.tenant_id,
        deny_action="tenant_profile_read_denied",
    )
    with vtr_connection(operator_id=operator) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, business_name, phase, plan_tier, business_type, locality, "
            "city_tier, language_preference, preferred_language, signed_up_at, trial_started_at, "
            "phase_entered_at, owner_name, whatsapp_last4, draft_attributes, draft_provenance, "
            "draft_created_at, draft_updated_at, onboarding_status, onboarding_queue_len, "
            "confirmed_fields, field_provenance "
            "FROM vtr_tenant_profile WHERE tenant_id = %s",
            (body.tenant_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    profile = rows[0] if rows else None
    logger.info(
        "vtr_tenant_profile OK operator=%s tenant=%s found=%s",
        operator,
        body.tenant_id,
        profile is not None,
    )
    return {"profile": profile}


@router.post("/api/orchestrator/ops/vtr-confirm-field")
def vtr_confirm_field(
    body: VtrConfirmFieldBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """VT-405 Part B (CL-441) — a VTR promotes ONE discovered field into the canonical business
    profile, marked VTR-asserted. ALL discovered fields are confirmable (incl. identity) per Fazal's
    ruling — no owner-only lane. Guardrails: the draft VALUE is server-read (never client-trusted,
    the VT-293/294 IDOR rule); the promote + provenance + audit commit in ONE transaction; provenance
    records source='vtr'; an `ops_audit` row is written (metadata only — field NAME, never the value);
    NO KG emit (VT-389). The owner's WhatsApp confirm still overwrites the VALUE via the same merge;
    the badge-supersede (flip status→owner_confirmed) is a forward-compatible follow-up.
    """
    _require_uuid(body.tenant_id, "tenant_id")
    field = body.field.strip()
    if not field or field == "_field_provenance":
        raise HTTPException(status_code=400, detail="invalid field")
    operator = _gate(
        x_internal_secret=x_internal_secret,
        x_operator_jwt=x_operator_jwt,
        body_operator_id=body.operator_id,
        tenant_id=body.tenant_id,
        deny_action="profile_confirm_denied",
        deny_target_kind="business_profile",
    )
    # ops_audit + l1_entities writes need the privileged role (app_vtr_role has neither grant); the
    # promote + provenance + audit are ONE atomic transaction (CL-390 — a guaranteed audit row).
    with get_pool().connection() as conn, conn.transaction():
        cur = conn.cursor()
        # Server-read the discovered value — the client NEVER supplies it (IDOR/PII).
        drow = cur.execute(
            "SELECT attributes -> %s AS value, (attributes ? %s) AS present "
            "FROM business_profile_draft WHERE tenant_id = %s",
            (field, field, body.tenant_id),
        ).fetchone()
        present = drow["present"] if isinstance(drow, dict) else (drow[1] if drow else False)
        if not drow or not present:
            raise HTTPException(status_code=404, detail="field not in the discovered draft")
        value = drow["value"] if isinstance(drow, dict) else drow[0]
        # Read-merge-write the nested _field_provenance map (top-level `||` would clobber it).
        erow = cur.execute(
            "SELECT attributes -> '_field_provenance' AS prov FROM l1_entities "
            "WHERE tenant_id = %s AND entity_type = 'business_profile' AND valid_to IS NULL",
            (body.tenant_id,),
        ).fetchone()
        prov_raw = (erow["prov"] if isinstance(erow, dict) else (erow[0] if erow else None)) if erow else None
        prov: dict[str, Any] = dict(prov_raw) if prov_raw else {}
        prov[field] = {
            "source": "vtr",
            "status": "vtr_confirmed",
            "confirmed_by": operator,
            "at": datetime.now(UTC).isoformat(),
        }
        cur.execute(
            "INSERT INTO l1_entities (tenant_id, entity_type, attributes) "
            "VALUES (%s, 'business_profile', %s) "
            "ON CONFLICT (tenant_id) WHERE entity_type = 'business_profile' "
            "DO UPDATE SET attributes = l1_entities.attributes || EXCLUDED.attributes",
            (body.tenant_id, Jsonb({field: value, "_field_provenance": prov})),
        )
        audit(
            cur,
            operator_id=operator,
            tenant_id=body.tenant_id,
            action="vtr_profile_confirm",
            target_kind="business_profile",
            target_id=body.tenant_id,
            detail=field,  # field NAME only — never the value (CL-390 metadata-only)
        )
    logger.info("vtr_confirm_field OK operator=%s tenant=%s field=%s", operator, body.tenant_id, field)
    return {"ok": True, "field": field, "status": "vtr_confirmed"}


@router.post("/api/orchestrator/ops/vtr-ownership-decision")
def vtr_ownership_decision(
    body: VtrOwnershipDecisionBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """VT-517 — a VTR human marks tenant ownership Verified or Rejected (replaces the zero-proof
    self-entered-number OTP). The decision flips the NON-BYPASSABLE execution gate
    (``tenants.ownership_verified``, read by the activation gate / Gate-0): only a VTR 'verified' lets
    an agent send/act. ONE atomic transaction — the tenants UPDATE + an ``ops_audit`` row + a
    fail-closed ``tm_audit`` row (VT-514): no decision without a guaranteed audit (the emit raises →
    the whole txn rolls back). CL-390: the audit carries the DECISION + booleans only, never the
    operator note / evidence text. IDOR-safe: ``_gate`` confirms the operator is assigned to the
    tenant before any write.
    """
    _require_uuid(body.tenant_id, "tenant_id")
    operator = _gate(
        x_internal_secret=x_internal_secret,
        x_operator_jwt=x_operator_jwt,
        body_operator_id=body.operator_id,
        tenant_id=body.tenant_id,
        deny_action="ownership_decision_denied",
        deny_target_kind="tenant",
    )
    # Deferred import — emit_tm_audit lazy-loads its DB deps (dep-less-smoke convention).
    from orchestrator.observability.tm_audit import emit_tm_audit

    verified = body.decision == "verified"
    note = _scrub_reason(body.note)         # free-text hygiene (CL-390)
    evidence = (body.evidence or "")[:500]  # URL/reference — clamp only
    with get_pool().connection() as conn, conn.transaction():
        cur = conn.cursor()
        cur.execute(
            "UPDATE tenants SET ownership_verified = %s, "
            "ownership_verified_at = CASE WHEN %s THEN now() ELSE NULL END, "
            "ownership_status = %s, ownership_reviewer_note = %s, "
            "ownership_reviewer_evidence = %s, ownership_reviewed_at = now(), "
            "ownership_reviewed_by = %s WHERE id = %s",
            (verified, verified, body.decision, note or None, evidence or None, operator, body.tenant_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="tenant not found")
        audit(
            cur,
            operator_id=operator,
            tenant_id=body.tenant_id,
            action="vtr_ownership_decision",
            target_kind="tenant",
            target_id=body.tenant_id,
            detail=body.decision,  # decision enum only — never note/evidence text (CL-390)
        )
        # Fail-closed TM audit (VT-514): can't-audit ⇒ can't-decide (raises → txn rollback).
        emit_tm_audit(
            event_layer="does",
            event_kind="ownership_decision",
            actor="vtr_operator",
            tenant_id=body.tenant_id,
            summary=f"VTR ownership decision: {body.decision}",
            decision={"decision": body.decision, "by": operator},
            action={"has_note": bool(note), "has_evidence": bool(evidence)},
            result={"ownership_verified": verified},
            severity="info",
            status="ok" if verified else "rejected",
            conn=conn,
        )
    logger.info(
        "vtr_ownership_decision OK operator=%s tenant=%s decision=%s",
        operator, body.tenant_id, body.decision,
    )
    return {"ok": True, "decision": body.decision, "ownership_verified": verified}


@router.post("/api/orchestrator/ops/vtr-draft-batches")
def vtr_draft_batches(
    body: VtrDraftBatchesBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """Draft batches for the tenant — aggregates ONLY (counts + template-name enums; params /
    owner_feedback / customer_id excluded at the view, plan §2). Bounded read."""
    _require_uuid(body.tenant_id, "tenant_id")
    operator = _gate(
        x_internal_secret=x_internal_secret,
        x_operator_jwt=x_operator_jwt,
        body_operator_id=body.operator_id,
        tenant_id=body.tenant_id,
        deny_action="draft_batches_denied",
    )
    capped = max(1, min(body.limit, _DRAFT_BATCH_PAGE_CAP))
    with vtr_connection(operator_id=operator) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT batch_id, tenant_id, tenant_name, agent, status, edit_cycles, created_at, "
            "updated_at, draft_count, pending_count, sent_count, skipped_count, halted_count, "
            "template_names "
            "FROM vtr_draft_batches WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s",
            (body.tenant_id, capped),
        )
        rows = [dict(r) for r in cur.fetchall()]
    logger.info(
        "vtr_draft_batches OK operator=%s tenant=%s rows=%d", operator, body.tenant_id, len(rows)
    )
    return {"rows": rows, "count": len(rows)}


@router.post("/api/orchestrator/ops/vtr-autonomy-override")
def vtr_autonomy_override_action(
    body: VtrAutonomyOverrideBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """Freeze / unfreeze / demote / revoke_l3 one (tenant, agent) via the Gap-6 seam. The mutation +
    its ops_audit row commit in ONE service-pool txn; freeze/demote/revoke cancel open batches
    atomically inside the seam (the binding kill-switch rule); unfreeze cancels nothing."""
    _require_uuid(body.tenant_id, "tenant_id")
    if body.agent not in OWNING_AGENTS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown agent {body.agent!r}; allowed: {sorted(OWNING_AGENTS)}",
        )
    reason = _scrub_reason(body.reason)
    target_id = f"{body.tenant_id}:{body.agent}"
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            operator = require_vtr_action(
                cur,
                x_internal_secret=x_internal_secret,
                x_operator_jwt=x_operator_jwt,
                body_operator_id=body.operator_id,
                tenant_id=body.tenant_id,
                deny_action="override_denied",
            )
        with conn.transaction():
            # Pre-count the open batches the seam is about to cancel (the seam returns state, not
            # the count; same-txn read so the audit metadata matches what the UPDATEs hit).
            batches_cancelled = 0
            if body.action != "unfreeze":  # unfreeze cancels nothing by design
                row = conn.execute(
                    "SELECT count(*) AS n FROM agent_draft_batches "
                    "WHERE tenant_id = %s AND agent = %s AND status = ANY(%s)",
                    (body.tenant_id, body.agent, list(_OPEN_BATCH_STATUSES)),
                ).fetchone()
                batches_cancelled = int(row["n"] if isinstance(row, dict) else row[0])
            state = vtr_autonomy_override(
                body.tenant_id,
                body.agent,
                body.action,
                reason=reason,
                vtr_id=operator,  # the VERIFIED claim id
                conn=conn,
            )
            with conn.cursor() as cur:
                audit(
                    cur,
                    operator_id=operator,
                    tenant_id=body.tenant_id,
                    action="autonomy_override",
                    target_kind="agent",
                    target_id=target_id,
                    detail=(
                        f"action={body.action} batches_cancelled={batches_cancelled} "
                        f"reason={reason[:200]}"
                    ),
                )
    logger.info(
        "vtr_autonomy_override OK operator=%s tenant=%s agent=%s action=%s batches_cancelled=%d",
        operator, body.tenant_id, body.agent, body.action, batches_cancelled,
    )
    return {
        "ok": True,
        "state": {
            "level": state.level,
            "frozen": state.frozen,
            "streak": state.clean_approval_streak,
        },
        "batches_cancelled": batches_cancelled,
    }


@router.post("/api/orchestrator/ops/vtr-batch-cancel")
def vtr_batch_cancel(
    body: VtrBatchCancelBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """Cancel ONE batch (the scalpel — one bad batch must not nuke a healthy agent the way freeze
    does): batch → 'cancelled', its drafted rows → 'halted'. Tenant DERIVED from the batch row
    server-side (VT-293/294); transport auth verified BEFORE the derive so an unauthenticated
    caller cannot probe batch existence."""
    _require_uuid(body.batch_id, "batch_id")
    verify_internal_secret(x_internal_secret)
    verify_operator_jwt(x_operator_jwt)
    reason = _scrub_reason(body.reason)
    pool = get_pool()
    with pool.connection() as conn:
        derived = _derive_batch_tenant(conn, body.batch_id)
        tenant_id, agent = derived["tenant_id"], derived["agent"]
        with conn.cursor() as cur:
            operator = require_vtr_action(
                cur,
                x_internal_secret=x_internal_secret,
                x_operator_jwt=x_operator_jwt,
                body_operator_id=body.operator_id,
                tenant_id=tenant_id,
                deny_action="batch_cancel_denied",
                deny_target_kind="draft_batch",
                deny_target_id=body.batch_id,
            )
        with conn.transaction():
            drafts_halted = cancel_batch(
                tenant_id, body.batch_id, reason=reason, vtr_id=operator, conn=conn
            )
            with conn.cursor() as cur:
                audit(
                    cur,
                    operator_id=operator,
                    tenant_id=tenant_id,
                    action="draft_batch_cancel",
                    target_kind="draft_batch",
                    target_id=body.batch_id,
                    detail=(
                        f"agent={agent} drafts_halted={drafts_halted} reason={reason[:200]}"
                    ),
                )
    logger.info(
        "vtr_batch_cancel OK operator=%s tenant=%s batch=%s drafts_halted=%d",
        operator, tenant_id, body.batch_id, drafts_halted,
    )
    return {"ok": True, "tenant_id": tenant_id, "drafts_halted": drafts_halted}


@router.post("/api/orchestrator/ops/vtr-batch-drafts")
def vtr_batch_drafts(
    body: VtrBatchDraftsBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """Exception-tier drill-in (Fazal=VTR#1 ONLY): per-draft template_name + params for one batch.

    Audit-before-read, SAME txn: the ``draft_params_reveal`` ops_audit row is INSERTed before the
    SELECT and commits with it — a reveal without its audit row is impossible (no silent
    break-glass). The read runs as ``SET LOCAL ROLE app_vtr_admin_role`` against the
    vtr_admin_batch_drafts view (the service role never SELECTs agent_drafts.params here; SET LOCAL
    auto-reverts at txn end on commit OR rollback)."""
    _require_uuid(body.batch_id, "batch_id")
    verify_internal_secret(x_internal_secret)
    verify_operator_jwt(x_operator_jwt)
    pool = get_pool()
    with pool.connection() as conn:
        derived = _derive_batch_tenant(conn, body.batch_id)
        tenant_id = derived["tenant_id"]
        with conn.cursor() as cur:
            operator = require_vtr_action(
                cur,
                x_internal_secret=x_internal_secret,
                x_operator_jwt=x_operator_jwt,
                body_operator_id=body.operator_id,
                tenant_id=tenant_id,
                deny_action="batch_drafts_denied",
                deny_target_kind="draft_batch",
                deny_target_id=body.batch_id,
            )
        require_exception_tier(operator)
        with conn.transaction(), conn.cursor() as cur:
            audit(
                cur,
                operator_id=operator,
                tenant_id=tenant_id,
                action="draft_params_reveal",
                target_kind="draft_batch",
                target_id=body.batch_id,
                detail=None,
            )
            cur.execute("SET LOCAL ROLE app_vtr_admin_role")
            cur.execute(
                "SELECT template_name, params, status, skip_reason "
                "FROM vtr_admin_batch_drafts WHERE tenant_id = %s AND batch_id = %s "
                "ORDER BY created_at, draft_id",
                (tenant_id, body.batch_id),
            )
            drafts = [dict(r) for r in cur.fetchall()]
    # CL-390: count only — params NEVER reach a log line.
    logger.info(
        "vtr_batch_drafts REVEAL operator=%s tenant=%s batch=%s drafts=%d",
        operator, tenant_id, body.batch_id, len(drafts),
    )
    return {"drafts": drafts}


# ---------------------------------------------------------------------------
# Screen — VTR strategy/behavioural directive ingest (VT-556 teach-loop)
# ---------------------------------------------------------------------------


@router.post("/api/orchestrator/ops/vtr-agent-directive")
def vtr_agent_directive(
    body: VtrAgentDirectiveBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """VT-556 — a VTR ingests a STRATEGY/BEHAVIOURAL directive the Team Manager picks up next run.

    Human-as-teacher input (NOT a draft correction — that is vtr-plan-edit): the verified VTR authors
    a directive that lands in agent_memory with provenance (operator id + authority='vtr') and is
    marked retrieval-eligible, so the manager reads it on its next dispatch (subject to the
    MANAGER_MEMORY_RETRIEVAL config gate). The ``_gate`` below IS the authority + tenant-scope check
    (require_vtr_action: the operator↔tenant assignment) — a VTR can only teach a tenant it is
    assigned to. Content is scrubbed here AND PII-redacted at the store. Fail-loud ops_audit row.
    """
    _require_uuid(body.tenant_id, "tenant_id")
    key = (body.memory_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="memory_key required")
    content = _scrub_reason(body.content)  # scrub_pii + clamp 500 (store also PII-redacts)
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    operator = _gate(
        x_internal_secret=x_internal_secret,
        x_operator_jwt=x_operator_jwt,
        body_operator_id=body.operator_id,
        tenant_id=body.tenant_id,
        deny_action="agent_directive_denied",
        deny_target_kind="agent_memory",
        deny_target_id=key,
    )
    from orchestrator.agents.agent_memory import upsert_directive

    stored_key = f"{body.directive_kind}:{key}"
    version = upsert_directive(
        body.tenant_id,
        memory_key=stored_key,
        content=content,
        authored_by_operator_id=operator,
        agent=body.agent,
        authority="vtr",
    )
    # Operator-attribution ops_audit row — NAMES/ids + version only, fail-LOUD (the plan-edit idiom).
    try:
        pool = get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            audit(
                cur,
                operator_id=operator,
                tenant_id=body.tenant_id,
                action="agent_directive",
                target_kind="agent_memory",
                target_id=key,
                detail=f"kind={body.directive_kind} agent={body.agent} v{version}",
            )
    except Exception as exc:
        logger.error(
            "vtr_agent_directive audit append FAILED operator=%s tenant=%s key=%s exc=%r",
            operator, body.tenant_id, key, exc,
        )
        raise HTTPException(
            status_code=500,
            detail=f"directive stored as v{version} but the audit append failed",
        ) from exc
    logger.info(
        "vtr_agent_directive OK operator=%s tenant=%s key=%s kind=%s v=%s",
        operator, body.tenant_id, key, body.directive_kind, version,
    )
    return {"ok": True, "version": version, "memory_key": stored_key}
