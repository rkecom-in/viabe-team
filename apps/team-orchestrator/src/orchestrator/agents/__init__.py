"""VT-369 Gap-5 — specialist agents + master coordinator.

The framework half of Gap-5: a deterministic daily coordinator sweep (zero-LLM,
Pillar 1) consumes the Gap-4 roadmap via ``business_plan.seams`` and dispatches
at most one work item per tenant per sweep into a per-item DBOS workflow. The
LLM lives in the specialist executors invoked BY that workflow — never in the
sweep. The registry is static and closed (``coordinator.get_registry``);
specialists implement the ``SpecialistAgent`` protocol and exchange IDs +
counters ONLY (the IDs-in-state rule — no names, bundles, or draft params in
workflow inputs/outputs; all PII is re-read from RLS tables).
"""
