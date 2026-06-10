"""VT-368 Gap-4 — the business-plan generator (grounding + LLM + the DBOS workflow).

Pipeline: ``_gather_grounding`` reads CONFIRMED-ONLY facts from L1 (the canonical
``business_profile`` entity — owner-confirmed by construction of the promotion gate — plus the
tenant entity and its ``has_listing`` platform listings) into a FROZEN ``{Fid: {key, value,
source}}`` fact bundle. Every number in the bundle is computed in Python — the LLM only arranges
facts, it never computes or invents them. ``_generate_and_validate`` resolves the ``business_plan``
models.yaml slot, prompts for strict JSON, then runs the citation validator
(``schema.validate_plan`` → ``schema.strip_violations`` → one retry with the violations appended →
``schema.degrade_template``). It NEVER returns an unvalidated plan and never raises into the
workflow. ``generate_business_plan_workflow`` persists via ``store.write_new_version`` (the
ms parent-lock mint — called AFTER generation, never around it) and hands off to delivery
best-effort.

HARD RULE: this module never reads the onboarding draft store — unconfirmed draft fields are NEVER
grounding facts (an import-graph test enforces this).
"""

from __future__ import annotations

import importlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import yaml
from dbos import DBOS

from orchestrator.business_plan import store
from orchestrator.knowledge.kg_vocab import EntityType, RelationshipType
from orchestrator.knowledge.l1 import (
    BUSINESS_PROFILE_ENTITY_TYPE,
    search_entities,
    traverse_relationships,
)
from orchestrator.observability import log as obs_log

logger = logging.getLogger(__name__)

GENERATED_BY = "gap4_generator"

_MODELS_YAML = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
_MAX_OUTPUT_TOKENS = 4096
_LLM_TIMEOUT_SECONDS = 60.0  # hot-ish path discipline — never hang a workflow on a slow LLM call
_LISTING_FETCH_LIMIT = 50
_MAX_LOGGED_VIOLATIONS = 20


@dataclass(frozen=True)
class Grounding:
    """The frozen grounding for ONE generation: ``bundle`` is the ``{Fid: {key, value, source}}``
    fact map persisted as ``fact_bundle_json`` (readers re-verify citations against it offline)."""

    bundle: dict[str, dict[str, Any]] = field(default_factory=dict)
    confirmed_profile: dict[str, Any] = field(default_factory=dict)
    business_name: str | None = None


# --- grounding ---------------------------------------------------------------


def _gather_grounding(tenant_id: UUID | str) -> Grounding:
    """CONFIRMED-only facts from L1. Sources: the canonical ``business_profile`` attributes
    (scalar fields only — nested context blobs are not citable facts), the tenant entity's
    business_name, and ``has_listing`` platform listings (platform + rating). The derived numbers
    (listing_count, avg rating) are computed HERE in Python — never by the LLM."""
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))

    profile_rows = search_entities(tid, entity_type=BUSINESS_PROFILE_ENTITY_TYPE, limit=1)
    confirmed_profile: dict[str, Any] = (
        dict(profile_rows[0].attributes or {}) if profile_rows else {}
    )

    tenant_rows = search_entities(tid, entity_type=EntityType.TENANT, limit=1)
    tenant_attrs: dict[str, Any] = dict(tenant_rows[0].attributes or {}) if tenant_rows else {}

    raw_name = confirmed_profile.get("business_name") or tenant_attrs.get("business_name")
    business_name = str(raw_name) if raw_name else None

    facts: list[tuple[str, Any, str]] = []
    for key in sorted(confirmed_profile):
        value = confirmed_profile[key]
        if isinstance(value, (str | int | float | bool)) and value != "":
            facts.append((key, value, "business_profile"))
    if business_name and "business_name" not in confirmed_profile:
        facts.append(("business_name", business_name, "tenant"))

    listings: list[dict[str, Any]] = []
    if tenant_rows:
        paths = traverse_relationships(
            tid,
            start_entity=tenant_rows[0].id,
            max_depth=1,
            relationship_type=RelationshipType.HAS_LISTING,
        )
        listing_ids = {p.entities[-1] for p in paths}
        if listing_ids:
            listings = [
                dict(ent.attributes or {})
                for ent in search_entities(
                    tid, entity_type=EntityType.PLATFORM_LISTING, limit=_LISTING_FETCH_LIMIT
                )
                if ent.id in listing_ids
            ]

    ratings: list[float] = []
    for listing in sorted(listings, key=lambda a: str(a.get("platform") or "")):
        platform = listing.get("platform")
        rating = listing.get("rating")
        if not platform:
            continue
        if isinstance(rating, (int | float)) and not isinstance(rating, bool):
            facts.append((f"{platform}_rating", float(rating), "platform_listing"))
            ratings.append(float(rating))
    if listings:
        facts.append(("listing_count", len(listings), "computed"))
    if ratings:
        facts.append(("avg_platform_rating", round(sum(ratings) / len(ratings), 2), "computed"))

    bundle = {
        f"F{i}": {"key": k, "value": v, "source": s} for i, (k, v, s) in enumerate(facts, start=1)
    }
    return Grounding(
        bundle=bundle, confirmed_profile=confirmed_profile, business_name=business_name
    )


# --- model resolution + LLM call ----------------------------------------------


def _resolve_plan_model() -> str:
    """models.yaml ``business_plan`` slot — VIABE_ENV=production → production model, else test
    (mirrors apify_food._resolve_theme_model)."""
    env = os.environ.get("VIABE_ENV", "test").lower()
    slot = "production" if env == "production" else "test"
    with open(_MODELS_YAML) as f:
        config = yaml.safe_load(f)
    return cast(str, config["business_plan"][slot])


def _call_llm(prompt: str, model: str) -> str:
    """One non-streaming Messages call; returns the concatenated text blocks."""
    from anthropic import Anthropic

    # max_retries=0: OUR retry-once-with-violations loop is the retry policy. The SDK default (2)
    # would compound it to a ~6-minute worst case (2 attempts × 3 HTTP tries × 60s) against the DBOS
    # 360s ceiling (adversarial-verify note).
    client = Anthropic(max_retries=0)
    resp = client.messages.create(
        model=model,
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
        timeout=_LLM_TIMEOUT_SECONDS,
    )
    return "".join(
        getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
    ).strip()


def _build_prompt(grounding: Grounding, violations: list[str] | None = None) -> str:
    """The constrained grounding prompt. Reference ONLY <facts>; cite [Fid]s everywhere; SCALE
    LENGTH TO FACTS (thin facts → a SHORTER honest plan, never 6 months of filler); strict JSON."""
    fact_lines = "\n".join(
        f"[{fid}] {f['key']} = {json.dumps(f['value'], ensure_ascii=False)} (source: {f['source']})"
        for fid, f in grounding.bundle.items()
    )
    name = grounding.business_name or "this business"
    agents = ", ".join(sorted(store.OWNING_AGENTS))
    prompt = f"""You write a grounded business summary and growth roadmap for a small Indian \
business owner ({name}). You may reference ONLY the facts inside <facts> below — nothing else.

<facts>
{fact_lines}
</facts>

RULES (strict):
- Every sentence of the summary and every roadmap item's "why" must cite at least one fact id \
in square brackets, e.g. [F1].
- Never state a number, rating, platform, category, or location that is not literally in <facts>.
- If a claim would need a fact that is absent, OMIT the claim entirely.
- SCALE LENGTH TO FACTS: with only a few facts, produce a SHORTER honest plan (2-3 months, fewer \
items). NEVER pad to 6 months of filler.
- "owning_agent" must be one of: {agents}. Use "unassigned" when unsure.
- "month" is an integer 1-6. "objective" is at most 120 characters.
- "headline_metrics" values must be copied LITERALLY from <facts> values.
- "text_hi" / "owner_action_hi" are natural Hindi (Devanagari) renderings.
- Set "owner_action"/"owner_action_hi" to null unless "owner_action_needed" is true.

Respond with ONLY this JSON object (no markdown, no prose):
{{"summary": {{"text": "...", "text_hi": "...", "cited_facts": ["F1"], \
"headline_metrics": {{}}}}, "roadmap": [{{"month": 1, "objective": "...", "why": "... [F1]", \
"cited_facts": ["F1"], "owning_agent": "unassigned", "owner_action_needed": false, \
"owner_action": null, "owner_action_hi": null}}]}}"""
    if violations:
        joined = "\n".join(f"- {v}" for v in violations)
        prompt += (
            "\n\nYOUR PREVIOUS ATTEMPT HAD THESE VIOLATIONS — fix every one, citing only the "
            f"facts in <facts>:\n{joined}"
        )
    return prompt


# --- parse + normalize ---------------------------------------------------------


def _parse_plan(raw: str) -> tuple[dict[str, Any], list[Any]] | None:
    """Strict-JSON parse (tolerating a stray code fence). None on any shape failure."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    summary = parsed.get("summary")
    roadmap = parsed.get("roadmap")
    if not isinstance(summary, dict) or not isinstance(roadmap, list):
        return None
    return summary, roadmap


def _coerce_month(value: Any) -> int:
    try:
        month = int(value)
    except (TypeError, ValueError):
        month = 1
    return min(max(month, 1), 6)


def _normalize(
    summary: dict[str, Any], roadmap: list[Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Shape the LLM output into the per-item contract: fresh uuid4 ``item_id``s, dense ``seq``
    1..N, status=proposed, provenance origin=llm_v1. Pure shaping — validation is schema's job."""
    norm_summary = {
        "text": str(summary.get("text") or ""),
        "text_hi": str(summary.get("text_hi") or ""),
        "cited_facts": [str(f) for f in (summary.get("cited_facts") or []) if f],
        "headline_metrics": dict(summary.get("headline_metrics") or {}),
    }
    items: list[dict[str, Any]] = []
    for raw_item in roadmap:
        if not isinstance(raw_item, dict):
            continue
        action_needed = bool(raw_item.get("owner_action_needed"))
        agent = raw_item.get("owning_agent")
        items.append(
            {
                "item_id": str(uuid4()),
                "seq": len(items) + 1,
                "month": _coerce_month(raw_item.get("month")),
                "objective": str(raw_item.get("objective") or "")[:120],
                "why": str(raw_item.get("why") or ""),
                "cited_facts": [str(f) for f in (raw_item.get("cited_facts") or []) if f],
                "owning_agent": agent if agent in store.OWNING_AGENTS else "unassigned",
                "owner_action_needed": action_needed,
                "owner_action": (
                    str(raw_item["owner_action"])
                    if action_needed and raw_item.get("owner_action")
                    else None
                ),
                "owner_action_hi": (
                    str(raw_item["owner_action_hi"])
                    if action_needed and raw_item.get("owner_action_hi")
                    else None
                ),
                "status": "proposed",
                "provenance": {
                    "origin": "llm_v1",
                    "editor": None,
                    "prev_version": None,
                    "diff_from_prev": None,
                },
            }
        )
    return norm_summary, items


# --- generate + validate ---------------------------------------------------------


def _generate_and_validate(
    tenant_id: UUID | str, grounding: Grounding, llm: Any = None
) -> dict[str, Any]:
    """Generate → validate → strip → retry-once (violations appended) → degraded template.

    NEVER returns an unvalidated plan and NEVER raises to the workflow: any LLM/parse/validation
    failure on both attempts falls through to ``schema.degrade_template`` (the deterministic,
    citation-safe floor) + a ``business_plan_generation_degraded`` event.
    """
    call = llm or _call_llm
    model = _resolve_plan_model()
    schema = importlib.import_module("orchestrator.business_plan.schema")

    violations: list[str] = []
    for attempt in (1, 2):
        prompt = _build_prompt(grounding, violations=violations if attempt == 2 else None)
        try:
            raw = call(prompt, model)
            parsed = _parse_plan(raw)
            if parsed is None:
                violations = ["invalid_json: output was not the required strict-JSON object"]
                continue
            summary, roadmap = _normalize(*parsed)
            violations = list(schema.validate_plan(summary, roadmap, grounding.bundle))
            if not violations:
                return {"summary": summary, "roadmap": roadmap, "model_id": model}
            summary, roadmap, remaining = schema.strip_violations(summary, roadmap, grounding.bundle)
            if not remaining:
                return {"summary": summary, "roadmap": roadmap, "model_id": model}
            violations = list(remaining)
        except Exception as exc:  # noqa: BLE001 — a generation failure must degrade, never raise
            violations = [f"generation_failed: {type(exc).__name__}: {exc}"]

    summary, roadmap = schema.degrade_template(grounding.bundle)
    obs_log.log_event(
        event_type="business_plan_generation_degraded",
        run_id=uuid4(),
        tenant_id=str(tenant_id),
        severity="warn",
        component="business_plan",
        payload={"violations": violations[:_MAX_LOGGED_VIOLATIONS], "model_id": model},
    )
    return {"summary": summary, "roadmap": roadmap, "model_id": model}


# --- the workflow ------------------------------------------------------------------


@DBOS.workflow()
def generate_business_plan_workflow(tenant_id: str) -> dict[str, Any]:
    """Gap-4 spine: triggered on onboarding-journey completion. Idempotent (a tenant with ANY plan
    version skips — regeneration is a different, explicit path), grounding-gated (no confirmed
    facts → skip, never an ungrounded plan), delivery best-effort (a send failure never fails the
    persisted version)."""
    run_id = uuid4()

    if store.plan_exists(tenant_id):
        return {"skipped": "exists"}

    grounding = _gather_grounding(tenant_id)
    if not grounding.confirmed_profile or not grounding.bundle:
        obs_log.log_event(
            event_type="business_plan_skipped",
            run_id=run_id,
            tenant_id=tenant_id,
            severity="info",
            component="business_plan",
            payload={"reason": "no_profile"},
        )
        return {"skipped": "no_profile"}

    result = _generate_and_validate(tenant_id, grounding)
    version = store.write_new_version(
        tenant_id,
        summary=result["summary"],
        roadmap=result["roadmap"],
        fact_bundle=grounding.bundle,
        generated_by=GENERATED_BY,
        model_id=result["model_id"],
    )

    try:
        from orchestrator.business_plan import delivery  # lazy — delivery lands separately

        delivery.deliver_plan(tenant_id, version)
    except Exception:  # noqa: BLE001 — delivery is best-effort; the version is already persisted
        logger.exception(
            "business_plan delivery failed (best-effort) tenant=%s version=%s", tenant_id, version
        )

    obs_log.log_event(
        event_type="business_plan_generated",
        run_id=run_id,
        tenant_id=tenant_id,
        severity="info",
        component="business_plan",
        payload={
            "version": version,
            "model_id": result["model_id"],
            "item_count": len(result["roadmap"]),
        },
    )
    return {"version": version}


__all__ = [
    "GENERATED_BY",
    "Grounding",
    "generate_business_plan_workflow",
]
