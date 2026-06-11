"""VT-366 Gap-2a — the Auto-Discovery Engine.

At signup-complete (post-commit, NOT in the request path) this fans a FIXED list of public sources
keyed on {business_name, business_type, city, whatsapp_number, website?} → assembles a DRAFT business
profile (``draft_profile`` — owner-confirmed before anything is asserted). Reuses the source adapters.

Guards (Cowork adj #3): a per-run COST CEILING circuit-breaker aborts a runaway (a source that
paginates/retries can't blow the budget), and the actual per-run cost is recorded to observability so
production spend is verifiable + drift-alertable. Fail-soft per source — one source down ≠ kill the run.
"""

from __future__ import annotations

import logging
from typing import Any, Callable
from uuid import UUID, uuid4

from dbos import DBOS

logger = logging.getLogger(__name__)


@DBOS.workflow()
def auto_discovery_workflow(tenant_id: str, seed: dict[str, Any]) -> dict[str, Any]:
    """DBOS background entrypoint (enqueued from the signup post-commit seam via
    ``DBOS.start_workflow`` — non-blocking, NOT in the request path). Thin wrapper so the body
    stays plain + unit-testable."""
    return auto_discovery_run(tenant_id, seed)

# Per-run economics. Estimate = GBP (~$0.004) + website Haiku (~$0.001). The ceiling is ~3.6× the
# estimate — a circuit-breaker, NOT the expected spend (adj #3). Serper is deferred (no cost yet).
_ESTIMATE_USD = 0.005
_COST_CEILING_USD = 0.018


def _hold_if_paused(tenant_id: UUID | str) -> int:
    """VT-374 per-source pause hold (kind 'auto_discovery'); returns paused_ms.

    Durable variant (checkpointed @DBOS.step reads + DBOS.sleep) inside the DBOS workflow
    body; plain poll for direct calls (tests/admin drive ``auto_discovery_run`` directly).
    check_pause inside never raises (F9 two-tier) — a control outage cannot kill a run."""
    from orchestrator import run_control

    if DBOS.workflow_id is not None:
        return run_control.hold_while_paused_durable(tenant_id, "auto_discovery")
    return run_control.hold_while_paused(tenant_id, "auto_discovery")


def _consume_skip_sources(tenant_id: UUID | str) -> tuple[frozenset[str], str | None]:
    """Consume-first claim of the (auto_discovery, source_fetch) one-shot override (F8/N2);
    returns (skip set, override_id). ``skip_sources`` is the sole allow-listed pin.

    Run identity for the N2 recovery-idempotent predicate = the DBOS workflow id when
    UUID-shaped (a recovered body re-applies the SAME row), else a fresh uuid4 (matches
    next-run pins only — the shape rerun.py registers for this kind). A control-DB
    failure proceeds with no skips, logged loudly — never a new path that kills the run."""
    try:
        from orchestrator import run_control
        from orchestrator.graph import get_pool

        wf_id = DBOS.workflow_id
        try:
            run_uuid = UUID(str(wf_id)) if wf_id else uuid4()
        except ValueError:
            run_uuid = uuid4()
        with get_pool().connection() as conn:
            override = run_control.consume_override(
                conn,
                tenant_id=tenant_id,
                workflow_kind="auto_discovery",
                step_name="source_fetch",
                run_id=run_uuid,
            )
    except Exception:  # noqa: BLE001 — control outage must not kill discovery (F9 spirit)
        logger.warning(
            "auto_discovery: override consume failed tenant=%s — no source skips",
            tenant_id,
            exc_info=True,
        )
        return frozenset(), None
    if override is None:
        return frozenset(), None
    raw = (override.pinned_input or {}).get("skip_sources") or []
    return frozenset(str(s) for s in raw), str(override.id)


def auto_discovery_run(
    tenant_id: UUID | str,
    seed: dict[str, Any],
    *,
    sources: list[Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Run the engine for one tenant. Fan the fixed source list (GBP first — it yields the website
    the website source then fetches), fail-soft per source, ABORT if cumulative cost exceeds the
    ceiling, record actual cost. Returns a summary {spent_usd, sources:{name:status}}."""
    if sources is None:
        from orchestrator.onboarding.auto_discovery_sources import (
            discover_gbp,
            discover_serper,
            discover_website,
        )

        sources = [discover_gbp, discover_website, discover_serper]  # GBP → website → serper

    seed = dict(seed)  # local copy; the GBP→website chain mutates it
    # VT-374 (auto_discovery, source_fetch) seam: one consume-first override claim per run
    # (skip_sources pin), then a per-source pause hold at the top of every iteration.
    skip_sources, override_id = _consume_skip_sources(tenant_id)
    paused_ms = 0
    spent = 0.0
    statuses: dict[str, str] = {}
    aborted = False
    for src in sources:
        if spent > _COST_CEILING_USD:
            logger.error(
                "auto_discovery: cost ceiling $%.4f EXCEEDED (spent $%.4f) tenant=%s — ABORT run",
                _COST_CEILING_USD, spent, tenant_id,
            )
            aborted = True
            break
        paused_ms += _hold_if_paused(tenant_id)
        name = getattr(src, "__name__", "source").replace("discover_", "")
        if name in skip_sources:
            statuses[name] = "skipped_by_override"
            continue
        try:
            result = src(tenant_id, seed)
            spent += getattr(result, "cost_usd", 0.0)
            statuses[name] = getattr(result, "status", "error")
            website = getattr(result, "website", None)
            if website and not seed.get("website"):
                seed["website"] = website  # GBP → website chain
        except Exception:  # noqa: BLE001 — one fragile source must not kill the run
            logger.exception("auto_discovery: source %s raised tenant=%s — fail-soft", name, tenant_id)
            statuses[name] = "error"

    # Run-control trail rides the auto_discovery_cost event (not the return dict — its
    # exact key set is a pinned contract for existing consumers/tests).
    _record_cost(
        tenant_id, spent, statuses, aborted, paused_ms=paused_ms, override_id=override_id
    )
    return {"tenant_id": str(tenant_id), "spent_usd": round(spent, 4), "aborted": aborted, "sources": statuses}


def _record_cost(
    tenant_id: UUID | str,
    spent: float,
    statuses: dict[str, str],
    aborted: bool,
    *,
    paused_ms: int = 0,
    override_id: str | None = None,
) -> None:
    """Record the actual per-run cost to observability (standing prod spend guard — adj #3). Best
    effort; a logging failure must not fail the engine."""
    try:
        from orchestrator.observability.log import log_event

        log_event(
            event_type="auto_discovery_cost",
            run_id=uuid4(),
            tenant_id=tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id)),
            severity="warning" if (aborted or spent > _ESTIMATE_USD * 3) else "info",
            component="onboarding",
            payload={
                "cost_usd": round(spent, 4),
                "estimate_usd": _ESTIMATE_USD,
                "ceiling_usd": _COST_CEILING_USD,
                "aborted_on_ceiling": aborted,
                "sources": statuses,
                # VT-374 run-control trail (IDs + counters only)
                "paused_ms": paused_ms,
                "override_id": override_id,
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("auto_discovery: cost record failed tenant=%s (spent $%.4f)", tenant_id, spent)
