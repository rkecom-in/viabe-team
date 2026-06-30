"""VT-507 — async parallel entity discovery (start / poll).  Internal-secret gated
(team-web proxies; all vendor calls happen orchestrator-side).

POST /api/orchestrator/onboard/discovery/start  {business_name, city}
  → {discovery_id}  (~50ms, never blocks on scrape/LLM)

GET  /api/orchestrator/onboard/discovery/{discovery_id}
  → {overall_status, sources: {llm: {...}, knowyourgst: {...}},
     candidates: [merged de-duped by GSTIN], both_complete_zero: bool}

Auth mirrors entity_match.py / drive_push.py (X-Internal-Secret / INTERNAL_API_SECRET).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)

_SOURCES = ("llm", "knowyourgst")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class DiscoveryStartBody(BaseModel):
    business_name: str
    city: str = ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/api/orchestrator/onboard/discovery/start")
async def discovery_start(
    body: DiscoveryStartBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    """Kick BOTH sources (LLM + knowyourgst) concurrently in the background, return a
    discovery_id immediately (~50ms). Never blocks on the scrape or LLM call.

    A cache hit at kick time means the source completes before the first poll."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})
    if not body.business_name.strip():
        raise HTTPException(status_code=422, detail={"code": "business_name_required"})

    discovery_id = uuid.uuid4()
    name = body.business_name.strip()
    city = (body.city or "").strip()

    _insert_running_rows(discovery_id, list(_SOURCES))

    loop = asyncio.get_running_loop()
    loop.create_task(_run_source(discovery_id, "knowyourgst", name, city))
    loop.create_task(_run_source(discovery_id, "llm", name, city))

    return {"discovery_id": str(discovery_id)}


@router.get("/api/orchestrator/onboard/discovery/{discovery_id}")
def discovery_poll(
    discovery_id: str,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    """Poll the discovery state for a given discovery_id.

    overall_status='searching' while either source is still running.
    both_complete_zero=True ONLY when BOTH sources completed with zero candidates
    (a source 'error' is NOT zero — the honest-empty signal requires both completed + empty)."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})

    rows = _read_source_rows(discovery_id)
    if not rows:
        raise HTTPException(status_code=404, detail={"code": "discovery_not_found"})

    sources: dict[str, Any] = {}
    all_candidates: list[dict[str, Any]] = []
    seen_gstins: set[str] = set()

    for source_name in _SOURCES:
        row = rows.get(source_name)
        if row is None:
            sources[source_name] = {"status": "running", "failure_reason": None, "candidates": []}
            continue
        status = row.get("status", "running")
        failure_reason = row.get("failure_reason")
        cands: list[dict[str, Any]] = row.get("candidates") or []
        if isinstance(cands, str):
            try:
                cands = json.loads(cands)
            except Exception:  # noqa: BLE001
                cands = []
        sources[source_name] = {
            "status": status,
            "failure_reason": failure_reason,
            "candidates": cands,
        }
        if status == "complete":
            for c in cands:
                key = (c.get("candidate_gstin") or "").upper()
                if key and key in seen_gstins:
                    continue
                if key:
                    seen_gstins.add(key)
                all_candidates.append(c)

    overall_complete = all(
        sources.get(s, {}).get("status") in ("complete", "error")
        for s in _SOURCES
    )
    overall_status = "complete" if overall_complete else "searching"

    llm_status = sources.get("llm", {}).get("status")
    kyg_status = sources.get("knowyourgst", {}).get("status")
    both_complete_zero = (
        llm_status == "complete"
        and kyg_status == "complete"
        and len(sources.get("llm", {}).get("candidates") or []) == 0
        and len(sources.get("knowyourgst", {}).get("candidates") or []) == 0
    )

    # VT-515: when both sources complete with zero candidates the signup is stuck
    # with no GSTIN to verify — surface this as a blocked_signup debug event.
    if both_complete_zero and overall_status == "complete":
        from orchestrator.observability.debug_log import emit_debug_event

        emit_debug_event(
            failure_type="silent_degrade",
            component="discovery",
            operation="both_complete_zero",
            error="Both knowyourgst and LLM discovery sources returned zero candidates",
            severity="error",
            impact="blocked_signup",
            trace_id=discovery_id,
        )

    return {
        "overall_status": overall_status,
        "sources": sources,
        "candidates": all_candidates,
        "both_complete_zero": both_complete_zero,
    }


# ---------------------------------------------------------------------------
# Background task: run one source, write results to DB
# ---------------------------------------------------------------------------

async def _run_source(
    discovery_id: uuid.UUID, source: str, name: str, city: str
) -> None:
    """Background coroutine: run one discovery source in a thread (blocking I/O), write
    to entity_discovery_requests when done. Never raises out — fail-soft per source."""
    t0 = time.monotonic()
    caught_exc: Exception | None = None
    try:
        loop = asyncio.get_running_loop()
        if source == "knowyourgst":
            candidates, failure_reason = await loop.run_in_executor(
                None, _fetch_knowyourgst, name, city
            )
        else:
            candidates, failure_reason = await loop.run_in_executor(
                None, _fetch_llm, name, city
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("discovery: source %s failed for %r", source, name, exc_info=True)
        candidates, failure_reason = [], "scrape_error"
        caught_exc = exc

    latency_ms = int((time.monotonic() - t0) * 1000)
    status = "error" if failure_reason else "complete"
    _update_source_row(discovery_id, source, status, failure_reason, candidates, latency_ms)

    # VT-515: emit a debug event for every non-happy-path outcome so the viewer
    # surfaces silent-degrades (no_key, zero_results) and vendor errors.
    _emit_source_event(
        discovery_id=discovery_id,
        source=source,
        failure_reason=failure_reason,
        candidates=candidates,
        latency_ms=latency_ms,
        exc=caught_exc,
    )


# ---------------------------------------------------------------------------
# Source fetchers (synchronous, run in thread pool)
# ---------------------------------------------------------------------------

def _fetch_knowyourgst(name: str, city: str) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch knowyourgst candidates for (name, city). Returns (candidates, failure_reason|None).

    The knowyourgst scraper already reads/writes discovery_cache (VT-507 L2 cache) so a
    repeated query returns in ms (no re-scrape). The matching layer (knowyourgst_match) runs
    on top of the scraper result to apply similarity filtering before returning candidates."""
    from orchestrator.integrations.methods.knowyourgst import (
        KnowYourGSTScraper,
        scraper_configured,
    )
    from orchestrator.integrations.methods.knowyourgst_match import (
        search_company_by_similar_name,
    )
    from orchestrator.onboarding.entity_match import EntityCandidate, _GSTIN_RE, _clean

    if not scraper_configured():
        return [], "no_key"
    scraper = KnowYourGSTScraper()
    try:
        rows = search_company_by_similar_name(scraper, name)
    except Exception:  # noqa: BLE001
        logger.warning("discovery: knowyourgst scrape failed for %r", name, exc_info=True)
        return [], "scrape_error"

    out: list[dict[str, Any]] = []
    for r in rows or []:
        gstin = (r.get("gst_number") or "").strip().upper()
        if not _GSTIN_RE.fullmatch(gstin):
            continue
        c = EntityCandidate(
            trade_name=_clean(r.get("company_name")) or name,
            source="knowyourgst",
            candidate_gstin=gstin,
            detail=_clean(r.get("state")),
        )
        out.append(asdict(c))
    return out, None


def _fetch_llm(name: str, city: str) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch LLM-discovery candidates for (name, city).  Returns (candidates, failure_reason|None).

    Calls _llm_candidates (VT-452) directly — that function already checks the LLM DB cache
    (VT-507) before making the Anthropic API call. A cache hit means no new LLM spend."""
    import os as _os
    from orchestrator.onboarding.entity_match import _llm_candidates

    if not _os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return [], "no_key"
    try:
        cands = _llm_candidates(name, city, llm_fn=None)
        return [asdict(c) for c in cands], None
    except Exception:  # noqa: BLE001
        logger.warning("discovery: LLM leg failed for %r", name, exc_info=True)
        return [], "scrape_error"


# ---------------------------------------------------------------------------
# DB I/O helpers (service-role pool, no tenant context)
# ---------------------------------------------------------------------------

def _insert_running_rows(discovery_id: uuid.UUID, sources: list[str]) -> None:
    """Insert status='running' rows for each source. Best-effort — poll returns 404 on DB failure."""
    try:
        from orchestrator.graph import get_pool
        with get_pool().connection() as conn, conn.cursor() as cur:
            for source in sources:
                cur.execute(
                    "INSERT INTO entity_discovery_requests (discovery_id, source, status)"
                    " VALUES (%s, %s, 'running')",
                    (str(discovery_id), source),
                )
    except Exception:  # noqa: BLE001
        logger.warning("discovery: failed to insert running rows for %s", discovery_id, exc_info=True)


def _update_source_row(
    discovery_id: uuid.UUID,
    source: str,
    status: str,
    failure_reason: str | None,
    candidates: list[dict[str, Any]],
    latency_ms: int,
) -> None:
    """Update the entity_discovery_requests row for (discovery_id, source) when the source completes."""
    try:
        from orchestrator.graph import get_pool
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE entity_discovery_requests
                SET status = %s, failure_reason = %s, candidates = %s::jsonb, latency_ms = %s
                WHERE discovery_id = %s AND source = %s
                """,
                (status, failure_reason, json.dumps(candidates), latency_ms, str(discovery_id), source),
            )
    except Exception:  # noqa: BLE001
        logger.warning(
            "discovery: failed to update row for %s/%s", discovery_id, source, exc_info=True
        )


def _read_source_rows(discovery_id_str: str) -> dict[str, dict[str, Any]]:
    """Return {source: row_dict} for all rows matching this discovery_id."""
    try:
        from orchestrator.graph import get_pool
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT source, status, failure_reason, candidates"
                " FROM entity_discovery_requests WHERE discovery_id = %s",
                (discovery_id_str,),
            )
            db_rows = cur.fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in db_rows:
            if isinstance(row, dict):
                result[row["source"]] = row
            else:
                result[row[0]] = {
                    "source": row[0],
                    "status": row[1],
                    "failure_reason": row[2],
                    "candidates": row[3],
                }
        return result
    except Exception:  # noqa: BLE001
        logger.warning("discovery: failed to read rows for %s", discovery_id_str, exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# VT-515: debug event emission for discovery failures
# ---------------------------------------------------------------------------

# failure_reason → (failure_type, component, vendor)
_FAILURE_REASON_MAP: dict[str, tuple[str, str, str | None]] = {
    "no_key":       ("silent_degrade", "discovery",    None),
    "scrape_error": ("vendor_error",   "scrapingbee",  "scrapingbee"),
    "timeout":      ("timeout",        "scrapingbee",  "scrapingbee"),
}
_SOURCE_COMPONENT: dict[str, str] = {
    "knowyourgst": "knowyourgst",
    "llm":         "anthropic",
}


def _emit_source_event(
    *,
    discovery_id: uuid.UUID,
    source: str,
    failure_reason: str | None,
    candidates: list[dict[str, Any]],
    latency_ms: int,
    exc: Exception | None,
) -> None:
    """VT-515: emit a debug_event for every non-happy discovery-source outcome.

    Covers:
    - no_key (ScrapingBee key / Anthropic key absent) → silent_degrade
    - scrape_error / timeout → vendor_error (the scraper or LLM call failed)
    - zero_results (source completed successfully but returned nothing) → silent_degrade
    - crash (the outer _run_source try-except caught an unhandled exception) → exception

    Happy path (failure_reason=None AND candidates present) → no emit.
    """
    from orchestrator.observability.debug_log import emit_debug_event

    tid = str(discovery_id)

    if exc is not None:
        # Unhandled exception from the background coroutine itself (rare crash path).
        emit_debug_event(
            failure_type="exception",
            component=_SOURCE_COMPONENT.get(source, "discovery"),
            operation=f"run_source_{source}",
            error=exc,
            severity="error",
            impact="degraded_to_manual",
            trace_id=tid,
            latency_ms=latency_ms,
        )
        return

    if failure_reason:
        ft, comp, vend = _FAILURE_REASON_MAP.get(
            failure_reason, ("vendor_error", _SOURCE_COMPONENT.get(source, "discovery"), None)
        )
        # no_key for the LLM leg should point at anthropic, not scrapingbee.
        if failure_reason == "no_key":
            comp = _SOURCE_COMPONENT.get(source, "discovery")
            vend = None
        emit_debug_event(
            failure_type=ft,
            component=comp,
            operation=failure_reason,
            error=failure_reason,
            severity="warning" if failure_reason == "no_key" else "error",
            impact="degraded_to_manual",
            trace_id=tid,
            vendor=vend,
            latency_ms=latency_ms,
        )
        return

    # No failure_reason but zero candidates → a "found nothing" silent degrade.
    if not candidates:
        emit_debug_event(
            failure_type="silent_degrade",
            component=_SOURCE_COMPONENT.get(source, "discovery"),
            operation="zero_results",
            error=f"{source} completed but returned zero candidates",
            severity="warning",
            impact="degraded_to_manual",
            trace_id=tid,
            latency_ms=latency_ms,
        )
