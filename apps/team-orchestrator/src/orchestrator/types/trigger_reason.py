"""Single source of truth for the orchestrator trigger-reason literal.

Imported by both context_builder.py and state/agent_graph_state.py — defining
it once prevents drift when a new trigger type lands.
"""

from typing import Literal

TriggerReason = Literal[
    "weekly_cadence",
    "owner_initiated",
    "edge_case_response",
    "support_followup",
]
