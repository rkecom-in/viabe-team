"""VT-368 Gap-4 — the Gap-5 CONSUME + Gap-6 EDIT seams over the versioned business plan.

Gap-5 specialist agents and the Gap-6 VTR surface import THIS module (+ ``store``) — never the LLM
``generator``. Every mutation appends a NEW immutable version via ``store.write_new_version`` (the
table IS the audit log; ``plan_history`` is the trail). The frozen ``fact_bundle`` is carried
forward UNCHANGED on every seam write: edits re-ground against the SAME facts the version cited —
no silent KG drift (whole-plan regenerate is the explicit refresh).

Security posture:
  - ``report_item_status`` does the ownership check SERVER-SIDE against the STORED row's
    ``owning_agent`` — never a caller-supplied scope (the VT-293/294 IDOR lesson).
  - ``edit_roadmap_item`` re-runs the deterministic citation validator against the frozen bundle
    whenever grounded text (``objective``/``why``) changes — a VTR cannot smuggle a hallucination;
    validation is NOT relaxed for human edits.
"""

from __future__ import annotations

import copy
from dataclasses import fields as _dataclass_fields
from typing import Any
from uuid import UUID

from orchestrator.business_plan import schema, store
from orchestrator.business_plan.store import (
    ITEM_STATUSES,
    OWNING_AGENTS,
    BusinessPlan,
    RoadmapItem,
)

# Patch keys a VTR may edit (Gap-6). Identity (item_id, seq), grounding (cited_facts) and audit
# (provenance) fields are NOT editable — the system mints those, never a caller.
EDITABLE_FIELDS = frozenset(
    {
        "objective",
        "why",
        "month",
        "owner_action",
        "owner_action_hi",
        "owner_action_needed",
        "status",
        "owning_agent",
    }
)

_ITEM_FIELD_NAMES = tuple(f.name for f in _dataclass_fields(RoadmapItem))


def _as_item(raw: dict[str, Any]) -> RoadmapItem:
    """Shape a raw roadmap_json entry into the ``RoadmapItem`` dataclass (unknown keys dropped —
    forward-compatible with additive schema growth)."""
    return RoadmapItem(**{k: raw[k] for k in _ITEM_FIELD_NAMES if k in raw})


def _locate(plan: BusinessPlan | None, item_id: str) -> tuple[int, dict[str, Any]]:
    """Index + raw dict of ``item_id`` in the latest plan's roadmap. KeyError if no plan exists or
    the id is not in the LATEST version (stale ids from superseded versions don't resolve)."""
    if plan is None:
        raise KeyError(f"no business plan exists for this tenant — item {item_id!r} not found")
    for idx, raw in enumerate(plan.roadmap):
        if raw.get("item_id") == str(item_id):
            return idx, raw
    raise KeyError(f"roadmap item {item_id!r} not in the latest plan (version {plan.version})")


def items_for_agent(
    tenant_id: UUID | str,
    owning_agent: str,
    *,
    statuses: tuple[str, ...] = ("accepted", "in_progress"),
) -> list[RoadmapItem]:
    """The Gap-5 consume read: the latest plan's items owned by ``owning_agent`` in one of
    ``statuses``, seq-ordered, as ``RoadmapItem`` dataclasses. No plan yet → ``[]`` (a specialist
    agent before Gap-4 fires simply has nothing to execute)."""
    if owning_agent not in OWNING_AGENTS:
        raise ValueError(
            f"unknown owning_agent {owning_agent!r}; allowed: {sorted(OWNING_AGENTS)}"
        )
    wanted = frozenset(statuses)
    unknown = wanted - ITEM_STATUSES
    if unknown:
        raise ValueError(
            f"unknown statuses {sorted(unknown)}; allowed: {sorted(ITEM_STATUSES)}"
        )
    plan = store.get_active_plan(tenant_id)
    if plan is None:
        return []
    picked = [
        raw
        for raw in plan.roadmap
        if raw.get("owning_agent") == owning_agent and raw.get("status") in wanted
    ]
    picked.sort(key=lambda raw: int(raw.get("seq", 0)))
    return [_as_item(raw) for raw in picked]


def report_item_status(
    tenant_id: UUID | str, item_id: str, new_status: str, *, agent: str
) -> int:
    """A Gap-5 agent advances ITS OWN item's status (e.g. accepted → in_progress → done). Appends a
    new immutable version (origin=``agent_status``) with the frozen fact_bundle + summary carried
    forward; siblings are untouched; ``item_id`` stays stable. Returns the new version.

    SERVER-SIDE ownership: the check reads ``owning_agent`` from the STORED latest row — the caller's
    ``agent`` identity is verified against it, never trusted as scope (the IDOR lesson). A mismatch
    raises ``PermissionError`` and mints nothing.
    """
    if new_status not in ITEM_STATUSES:
        raise ValueError(
            f"unknown status {new_status!r}; allowed: {sorted(ITEM_STATUSES)}"
        )
    latest = store.get_active_plan(tenant_id)
    idx, raw = _locate(latest, item_id)
    owner = raw.get("owning_agent")
    if owner != agent:
        raise PermissionError(
            f"agent {agent!r} does not own roadmap item {item_id!r} "
            f"(owned by {owner!r}) — status report refused"
        )
    assert latest is not None  # _locate raised otherwise; narrows the type
    old_status = raw.get("status")
    roadmap = copy.deepcopy(latest.roadmap)
    target = roadmap[idx]
    target["status"] = new_status
    target["provenance"] = {
        "origin": "agent_status",
        "editor": agent,
        "prev_version": latest.version,
        "diff_from_prev": {"status": [old_status, new_status]},
    }
    return store.write_new_version(
        tenant_id,
        summary=latest.summary,
        roadmap=roadmap,
        fact_bundle=latest.fact_bundle,  # SAME frozen facts — a status advance re-grounds nothing
        generated_by=agent,
    )


def _validate_patch_values(patch: dict[str, Any]) -> None:
    """Contract-shape checks on the patched values (the per-item JSON shape is the Gap-5/6
    contract — a VTR edit must not be able to break it)."""
    problems: list[str] = []
    if "status" in patch and patch["status"] not in ITEM_STATUSES:
        problems.append(f"status {patch['status']!r} not in {sorted(ITEM_STATUSES)}")
    if "owning_agent" in patch and patch["owning_agent"] not in OWNING_AGENTS:
        problems.append(
            f"owning_agent {patch['owning_agent']!r} not in {sorted(OWNING_AGENTS)}"
        )
    if "month" in patch:
        month = patch["month"]
        if not isinstance(month, int) or isinstance(month, bool) or not 1 <= month <= 6:
            problems.append(f"month {month!r} must be an int in 1..6")
    if "objective" in patch:
        objective = patch["objective"]
        if not isinstance(objective, str) or not objective.strip() or len(objective) > 120:
            problems.append("objective must be a non-empty str of ≤120 chars")
    if "why" in patch and (not isinstance(patch["why"], str) or not patch["why"].strip()):
        problems.append("why must be a non-empty str")
    if "owner_action_needed" in patch and not isinstance(patch["owner_action_needed"], bool):
        problems.append("owner_action_needed must be a bool")
    for key in ("owner_action", "owner_action_hi"):
        if key in patch and patch[key] is not None and not isinstance(patch[key], str):
            problems.append(f"{key} must be a str or None")
    if problems:
        raise ValueError("invalid patch values: " + "; ".join(problems))


class StaleVersion(RuntimeError):
    """VT-370: the optimistic-concurrency rejection — the caller's expected_prev_version no longer
    matches the latest plan (a replayed request or a two-VTR lost-update race). Maps to HTTP 409."""


def edit_roadmap_item(
    tenant_id: UUID | str, item_id: str, patch: dict[str, Any], *, vtr_id: str,
    expected_prev_version: int | None = None,
) -> int:
    """The Gap-6 VTR edit: patch ONE item in the latest plan and append a new immutable version
    (origin=``vtr_edit``, ``diff_from_prev`` per actually-changed field, ``item_id`` stable, the
    SAME frozen fact_bundle carried forward). Returns the new version.

    RE-GROUND (validation NOT relaxed): when grounded text (``objective``/``why``) changes, the
    deterministic citation validator re-runs against the FROZEN bundle this version cited — an edit
    that introduces an uncited number/proper-noun is rejected with a ``ValueError`` listing the
    violations. A VTR cannot smuggle a hallucination past the guard the LLM is held to.
    """
    bad_keys = set(patch) - EDITABLE_FIELDS
    if bad_keys:
        raise ValueError(
            f"non-editable patch keys {sorted(bad_keys)}; allowed: {sorted(EDITABLE_FIELDS)}"
        )
    if not patch:
        raise ValueError("empty patch — nothing to edit")
    _validate_patch_values(patch)

    latest = store.get_active_plan(tenant_id)
    idx, raw = _locate(latest, item_id)
    assert latest is not None  # _locate raised otherwise; narrows the type
    # VT-370 optimistic concurrency: plan-edit is NOT idempotent (every call appends an immutable
    # version) — a replayed request or a two-VTR race must lose, not mint a duplicate/lost-update
    # version. The mint's parent-row lock serializes writers; this compare rejects the stale one.
    if expected_prev_version is not None and latest.version != expected_prev_version:
        raise StaleVersion(
            f"plan moved: latest=v{latest.version}, caller expected v{expected_prev_version}"
        )

    changed = {k: [raw.get(k), v] for k, v in patch.items() if raw.get(k) != v}
    editor = f"vtr:{vtr_id}"
    roadmap = copy.deepcopy(latest.roadmap)
    target = roadmap[idx]
    target.update(copy.deepcopy(patch))
    target["provenance"] = {
        "origin": "vtr_edit",
        "editor": editor,
        "prev_version": latest.version,
        "diff_from_prev": changed,
    }

    # Re-ground on ANY owner-visible text change — objective/why AND owner_action/_hi (the action
    # prompt is DELIVERED to the owner; without this a VTR smuggles a fabricated number through it —
    # adversarial-verify Probe-2).
    _GROUNDED_TEXT_FIELDS = ("objective", "why", "owner_action", "owner_action_hi")
    if any(f in changed for f in _GROUNDED_TEXT_FIELDS):
        violations = schema.validate_plan(latest.summary, roadmap, latest.fact_bundle)
        if violations:
            raise ValueError(
                f"edit rejected — grounding violations against the frozen fact bundle: {violations}"
            )

    return store.write_new_version(
        tenant_id,
        summary=latest.summary,
        roadmap=roadmap,
        fact_bundle=latest.fact_bundle,  # frozen — edits never absorb new KG state silently
        generated_by=editor,
    )
