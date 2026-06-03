"""VT-211 — Integration Agent onboarding step endpoint.

POST /api/orchestrator/integrations/onboard-step

Called by team-web's /api/onboard/answer route when the owner submits a
form on /team/onboard. Invokes the integration_agent subgraph directly
(NOT via supervisor brain — per Cowork review-verdict 2026-05-28
correction 2: a web-driven click is an explicit handoff; spending a
brain pass to re-decide the route would waste ~30 paise per click and
allow ambiguity). Persists phase transitions via the agent's tool calls.
Returns the next prompt the page should render.

Per VT-181: wrap the invoke in ``observability_context`` so pipeline_
steps envelopes carry the right run/tenant context.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)
router = APIRouter()


def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


class OnboardStepBody(BaseModel):
    tenant_id: str
    answer: str


class BusinessProfileSaveBody(BaseModel):
    tenant_id: str
    # Owner-edited fields from the wizard Review-&-Confirm step. Validated against an
    # allowlist below — the wizard may ONLY edit the owner-facing identity fields, never
    # arbitrary L1 attributes (e.g. derived archetype / enrichment keys).
    attributes: dict[str, Any]


# VT-267 PR-C: the only L1 business_profile keys the owner wizard may write. Everything else
# (derived archetype, *_context enrichment from apify_gbp/swiggy/zomato, agent reflections) is
# off-limits to the web edit surface. upsert_business_profile MERGEs (not clobbers), so writing
# only these keys preserves the enrichment siblings.
_WIZARD_EDITABLE_PROFILE_KEYS = frozenset(
    {"business_name", "business_type", "preferred_language", "owner_curated_context"}
)


def _open_run(tenant_id: UUID) -> UUID:
    """Open a pipeline_runs row for observability of this onboarding turn."""
    run_id = uuid4()
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status, trigger_kind) "
            "VALUES (%s, %s, 'running', 'web_onboard_step')",
            (str(run_id), str(tenant_id)),
        )
    return run_id


def _close_run(run_id: UUID, status: str) -> None:
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status = %s, ended_at = now() WHERE id = %s",
            (status, str(run_id)),
        )


def _read_next_prompt(tenant_id: UUID) -> tuple[str, str | None]:
    """Re-read tenant_integration_state to return ``(phase, next_prompt)``."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT phase, pending_owner_input "
            "FROM tenant_integration_state WHERE tenant_id = %s",
            (str(tenant_id),),
        )
        raw = cur.fetchone()
    if raw is None:
        return ("phase_1_discovery", None)
    row = cast("dict[str, Any]", raw)
    next_prompt: str | None = None
    pending = row.get("pending_owner_input")
    if isinstance(pending, dict):
        next_prompt = pending.get("prompt_text")
    return (row["phase"], next_prompt)


@router.post("/api/orchestrator/integrations/onboard-step")
async def onboard_step(
    body: OnboardStepBody,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=401, detail="invalid internal secret")
    try:
        tenant_uuid = UUID(body.tenant_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid tenant_id") from None

    # Late import — keeps test fixtures that don't need the agent fast.
    from langchain_core.messages import HumanMessage

    from orchestrator.agent.integration_agent import integration_agent
    from orchestrator.observability.decorators import observability_context

    run_id = _open_run(tenant_uuid)
    invocation_status = "completed"
    try:
        with observability_context(run_id=run_id, tenant_id=tenant_uuid):
            integration_agent.invoke({
                "messages": [HumanMessage(content=body.answer)],
                "tenant_id": tenant_uuid,
                "run_id": run_id,
                "trigger_reason": "owner_substantive_message",
            })
    except Exception:  # noqa: BLE001 — surface as 500-shape, log + close run
        logger.exception(
            "onboard_step: integration_agent.invoke failed; tenant=%s run=%s",
            tenant_uuid, run_id,
        )
        invocation_status = "failed"
        _close_run(run_id, invocation_status)
        raise HTTPException(status_code=500, detail="agent_invoke_failed") from None
    _close_run(run_id, invocation_status)

    next_phase, next_prompt = _read_next_prompt(tenant_uuid)
    return {
        "ok": True,
        "next_phase": next_phase,
        "next_prompt": next_prompt,
        "run_id": str(run_id),
    }


@router.post("/api/orchestrator/integrations/onboard/business-profile")
def save_business_profile(
    body: BusinessProfileSaveBody,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """VT-267 PR-C — persist owner-edited business_profile fields from the wizard.

    Validated against the editable allowlist, then MERGEd into the tenant's single L1
    'business_profile' entity via ``upsert_business_profile`` (RLS-scoped, MERGE-not-clobber,
    so enrichment siblings survive). team-web calls this server-side after authenticating the
    owner session; INTERNAL_API_SECRET gates it. CL-390: business identity, not customer PII.
    """
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=401, detail="invalid internal secret")
    try:
        tenant_uuid = UUID(body.tenant_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid tenant_id") from None

    # Reject any key outside the owner-editable allowlist (don't let the web surface write
    # derived/enrichment L1 attributes).
    unknown = set(body.attributes) - _WIZARD_EDITABLE_PROFILE_KEYS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"non-editable profile keys rejected: {sorted(unknown)}",
        )
    if not body.attributes:
        raise HTTPException(status_code=400, detail="no editable attributes provided")

    from orchestrator.knowledge import upsert_business_profile

    entity_id = upsert_business_profile(tenant_uuid, body.attributes)
    logger.info(
        "save_business_profile tenant=%s keys=%s entity=%s",
        tenant_uuid, sorted(body.attributes), entity_id,
    )
    return {"ok": True, "entity_id": str(entity_id)}
