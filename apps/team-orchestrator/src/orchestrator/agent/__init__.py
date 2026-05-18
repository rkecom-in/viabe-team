"""Orchestrator-agent package (VT-3.9).

The orchestrator-agent is the Stage-2 reasoning brain — it runs ON the LangGraph
substrate and decides routing for the residual ~30% of events the deterministic
Pre-Filter Gate hands up. Pillar 1: reasoning lives here, never in the
deterministic subtree; this package must not import the phase machine
(transitions / invariants) — CI enforces it.
"""

from orchestrator.agent.orchestrator_agent import (
    ORCHESTRATOR_AGENT_SYSTEM_PROMPT,
    orchestrator_agent,
)

__all__ = ["ORCHESTRATOR_AGENT_SYSTEM_PROMPT", "orchestrator_agent"]
