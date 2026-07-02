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

# Per-run economics. Estimate = GBP fetch (~$0.004) + VT-568 entity-resolution adjudication (one
# claude-opus-4-8 call + bounded web_search, ~$0.025) + website Haiku (~$0.001); GST reuses the paid
# verify lookup (no incremental cost). The ceiling is a circuit-breaker (a source that
# paginates/retries can't blow the budget), NOT the expected spend (adj #3). Serper deferred.
_ESTIMATE_USD = 0.030
_COST_CEILING_USD = 0.060


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
            discover_gst,
            discover_serper,
            discover_website,
        )

        # VT-568 — GST → GBP → website → serper. GST is placed FIRST so its verified identity anchors
        # (owner's legal/trade name + principal locality) are seeded BEFORE GBP adjudicates which Maps
        # candidate, if any, is the owner's company (the RKeCom "wrong company" fix — GBP must no longer
        # take items[0] blind). GST self-skips cleanly when the seed carries no verified gstin (VT-407),
        # so a non-GSTIN tenant just adjudicates GBP against the signup name alone.
        sources = [discover_gst, discover_gbp, discover_website, discover_serper]

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
            # VT-568 — a source's identity anchors feed a downstream source (GST anchors → GBP
            # adjudication). setdefault: never overwrite an anchor the seed already carries.
            for key, value in (getattr(result, "seed_updates", None) or {}).items():
                seed.setdefault(key, value)
            website = getattr(result, "website", None)
            if website and not seed.get("website"):
                seed["website"] = website  # GBP (resolved) → website chain
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


# --------------------------------------------------------------------------- VT-568 redrive


def redrive_discovery(tenant_id: UUID | str, *, seed: dict[str, Any] | None = None) -> dict[str, Any]:
    """VT-568 — re-run auto-discovery for an EXISTING tenant with a FRESH entity resolution.

    Rebuilds the seed from the tenant's SERVER-VERIFIED identity (verified name + GSTIN + signup
    business_type), RESETS the current draft row so a prior wrong-company run's stale GBP fields
    (category / website / about from a mis-adjudicated listing like Reecomps) cannot survive
    ``write_draft``'s merge, then re-fans the sources under the corrected ``discover_gst → discover_gbp``
    order so the draft is rewritten with the RIGHT company. Idempotent — the draft is one row per tenant
    (upsert), never duplicated.

    Scope: refreshes the DRAFT (owner-confirmed later). It does NOT mutate ``tenants.business_type`` —
    that stays the owner's signup choice / the owner-confirm gate's job (the mis-category only ever
    lived in the draft as a confirm hint, never on the tenant). Runs SYNCHRONOUSLY (admin/ops redrive
    of one tenant, not the DBOS bg workflow). The adjudicator needs a live ANTHROPIC_API_KEY, so run it
    on DEPLOYED DEV (Railway holds the key) per CL-2026-06-29 — a dead local key degrades to a
    fail-closed reject-all-GBP, not a fresh resolution. ``seed`` override is for tests."""
    resolved_seed = seed if seed is not None else _rebuild_seed(tenant_id)
    _reset_draft(tenant_id)
    return auto_discovery_run(tenant_id, resolved_seed)


def _rebuild_seed(tenant_id: UUID | str) -> dict[str, Any]:
    """Reconstruct the discovery seed for an existing tenant from its verified identity. Anchors on the
    Sandbox-verified name (falls back to the signup name) + the verified GSTIN, mirroring the signup
    kick's seed (VT-406)."""
    from orchestrator.graph import get_pool

    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT business_name, verified_business_name, gstin, business_type"
            " FROM tenants WHERE id = %s",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        raise ValueError(f"redrive_discovery: tenant {tenant_id} not found")

    def _col(key: str, idx: int) -> Any:
        return row[key] if isinstance(row, dict) else row[idx]

    seed: dict[str, Any] = {}
    business_name = _col("verified_business_name", 1) or _col("business_name", 0)
    if business_name:
        seed["business_name"] = business_name
    if (gstin := _col("gstin", 2)):
        seed["gstin"] = gstin
    if (business_type := _col("business_type", 3)):
        seed["business_type"] = business_type
    return seed


def _reset_draft(tenant_id: UUID | str) -> None:
    """Empty the tenant's draft (attributes + provenance) so a redrive rebuilds it clean. Uses the SAME
    RLS-scoped tenant_connection + UPDATE grant ``write_draft`` relies on (no DELETE grant assumed); a
    missing row is a 0-row no-op (the redrive's first write_draft INSERTs it)."""
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE business_profile_draft SET attributes = '{}'::jsonb,"
            " provenance = '{}'::jsonb, updated_at = now() WHERE tenant_id = %s",
            (str(tenant_id),),
        )
