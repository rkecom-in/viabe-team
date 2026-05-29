"""VT-196 — L0 production write wiring.

Wraps `orchestrator.observability.l0_memory.write_l0_fragment` with:
- consent gate (tenant.owner_inputs must be true; CL-390)
- async DBOS workflow (never blocks the agent response path)

The underlying L0 substrate (VT-126) already has read-side k-anonymity
(observation_count >= 10) + PII gate. Write-side per-tenant k-anonymity
admission needs schema changes (per-tenant contributor tracking) which
brief scope-locks out of this row. Surface in PR description as a
follow-up substrate question.
"""

from orchestrator.memory.l0_writer import write_l0_fragment_workflow

__all__ = ["write_l0_fragment_workflow"]
