"""VT-374 run-control step registry (plan §1/§4; build-contract pinned names).

Two honest tiers (F1): ``controllable`` = a true pre-execution call seam exists
(pause + override + re-run-from apply); ``observed`` = timeline display only —
the panel labels these non-controllable. Every entry maps to the dotted path of
the module that implements its seam (``STEP_IMPL_MODULES``); import raises if a
controllable entry's module appears in the gate manifest (F14) — a send/consent
gate can never be registered as controllable, even by a future edit.

Entries follow the STEP-0 seam inventory (.viabe/queue/VT-374/step0-report.md):
``question_brain_compose`` is DEMOTED to observed (owner-inbound hot path with a
blanket fail-open except — a hold there silently falls through or stalls the
owner's WhatsApp reply); ``dispatch_brain`` is PAUSE-ONLY (N3: empty
allowed_keys + pure_return False means no override can ever validate for it).

STDLIB-ONLY by design (dep-less CI smoke imports this module).
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.run_control.gate_manifest import GATE_MODULES


@dataclass(frozen=True)
class StepEntry:
    workflow_kind: str
    step_name: str
    tier: str  # 'controllable' | 'observed'
    pause_deny: bool = False  # True = pause never holds this step (I6 compliance paths)
    allowed_keys: frozenset[str] = frozenset()
    pure_return: bool = False  # pinned_output legal (v1: ALL False)
    inputs_redacted_at_write: bool = False


# The migration-131 workflow_controls CHECK list — the only legal workflow kinds.
WORKFLOW_KINDS: frozenset[str] = frozenset(
    {
        "webhook_inbound",
        "agent_dispatch",
        "auto_discovery",
        "plan_generate",
        "plan_deliver",
        "trial_sweep",
        "ingestion",
        "campaign_send",
    }
)

# (entry, implementing module dotted path). The module is the SEAM site (where the
# hold/consume executes), not the downstream brain — e.g. the pre-dispatch_brain
# pause boundary lives in orchestrator.runner (runner.py:591, N3).
_ENTRIES: tuple[tuple[StepEntry, str], ...] = (
    (
        # PAUSE-ONLY (N3): allowed_keys=∅ + pure_return=False → the ops API can never
        # validate an override write for this step (422 by construction).
        # inputs_redacted_at_write: the webhook_received envelope has body popped +
        # phone hashed at the write boundary (STEP-0 §3.2) — harness replays warn.
        StepEntry(
            "webhook_inbound",
            "dispatch_brain",
            "controllable",
            inputs_redacted_at_write=True,
        ),
        "orchestrator.runner",
    ),
    (
        # STEP-0 demotion: observed + pause-deny (fail-open except at journey.py:315
        # would swallow a raising hold; a blocking hold stalls the owner reply).
        StepEntry("webhook_inbound", "question_brain_compose", "observed", pause_deny=True),
        "orchestrator.onboarding.journey",
    ),
    (
        StepEntry("agent_dispatch", "execute_item", "controllable"),
        "orchestrator.agents.coordinator",
    ),
    (
        StepEntry(
            "agent_dispatch",
            "candidate_build",
            "controllable",
            allowed_keys=frozenset({"limit"}),
        ),
        "orchestrator.agents.sales_recovery_executor",
    ),
    (
        StepEntry(
            "agent_dispatch",
            "compose_drafts",
            "controllable",
            allowed_keys=frozenset({"model"}),
        ),
        "orchestrator.agents.sales_recovery_executor",
    ),
    (
        # Hold BEFORE persist — never between persist and arm (an unarmed
        # awaiting_approval batch violates the _cancel_batch invariant, STEP-0).
        StepEntry("agent_dispatch", "persist_batch", "controllable"),
        "orchestrator.agents.sales_recovery_executor",
    ),
    (
        StepEntry(
            "auto_discovery",
            "source_fetch",
            "controllable",
            allowed_keys=frozenset({"skip_sources"}),
        ),
        "orchestrator.onboarding.auto_discovery",
    ),
    (
        StepEntry("plan_generate", "generate_validate", "controllable"),
        "orchestrator.business_plan.generator",
    ),
    (
        StepEntry("plan_deliver", "deliver_parts", "controllable"),
        "orchestrator.business_plan.delivery",
    ),
    (
        StepEntry("trial_sweep", "evaluate_tenant", "controllable"),
        "orchestrator.billing.trial_sweep",
    ),
    (
        StepEntry("ingestion", "connector_pull", "controllable"),
        "orchestrator.integrations.scheduler",
    ),
    (
        # VT-300 supersession (N1 RETIRE arm): the supervisor campaign-send hold
        # migrates onto workflow_controls via this entry.
        StepEntry("campaign_send", "execute_fanout", "controllable"),
        "orchestrator.supervisor",
    ),
)

REGISTRY: dict[tuple[str, str], StepEntry] = {}
STEP_IMPL_MODULES: dict[tuple[str, str], str] = {}

for _entry, _module in _ENTRIES:
    _key = (_entry.workflow_kind, _entry.step_name)
    if _key in REGISTRY:
        raise RuntimeError(f"run_control registry: duplicate step entry {_key!r}")
    if _entry.workflow_kind not in WORKFLOW_KINDS:
        raise RuntimeError(
            f"run_control registry: {_key!r} uses unknown workflow_kind "
            f"{_entry.workflow_kind!r} (not in the migration-131 CHECK list)"
        )
    if _entry.tier not in ("controllable", "observed"):
        raise RuntimeError(f"run_control registry: {_key!r} has invalid tier {_entry.tier!r}")
    REGISTRY[_key] = _entry
    STEP_IMPL_MODULES[_key] = _module

# F14 import-time gate: a controllable step must never be implemented by a module
# in the gate manifest. Raising here (not warning) is the point — the process that
# would have honoured the bad registration refuses to boot instead.
for _key, _entry in REGISTRY.items():
    if _entry.tier == "controllable" and STEP_IMPL_MODULES[_key] in GATE_MODULES:
        raise RuntimeError(
            f"run_control registry: controllable step {_key!r} maps to gate module "
            f"{STEP_IMPL_MODULES[_key]!r} — send/consent/approval surfaces are "
            "structurally non-controllable (plan §4 F14)"
        )

# Re-run side-effect policy per workflow_kind (I8/F11). 'forbidden' refuses at
# /rerun: webhook_inbound (MessageSid ledger semantics — preserved sid dupe-no-ops,
# fresh sid double-sends), trial_sweep (warn-path has no send ledger), campaign_send
# (KG outbox dup, no value).
KIND_RERUN_POLICY: dict[str, str] = {
    "webhook_inbound": "forbidden",
    "agent_dispatch": "re-emit",
    "auto_discovery": "reuse",
    "plan_generate": "reuse",
    "plan_deliver": "reuse",
    "trial_sweep": "forbidden",
    "ingestion": "reuse",
    "campaign_send": "forbidden",
}

RERUNNABLE: frozenset[str] = frozenset(
    {"agent_dispatch", "auto_discovery", "plan_generate", "plan_deliver", "ingestion"}
)

if set(KIND_RERUN_POLICY) != set(WORKFLOW_KINDS):
    raise RuntimeError("run_control registry: KIND_RERUN_POLICY must cover every workflow_kind")
if any(v not in ("reuse", "re-emit", "forbidden") for v in KIND_RERUN_POLICY.values()):
    raise RuntimeError("run_control registry: invalid KIND_RERUN_POLICY value")
if RERUNNABLE != frozenset(k for k, v in KIND_RERUN_POLICY.items() if v != "forbidden"):
    raise RuntimeError(
        "run_control registry: RERUNNABLE must be exactly the non-forbidden kinds"
    )

__all__ = [
    "KIND_RERUN_POLICY",
    "REGISTRY",
    "RERUNNABLE",
    "STEP_IMPL_MODULES",
    "WORKFLOW_KINDS",
    "StepEntry",
]
