"""VT-268 — agent tool-surface guardrail (fail-closed enforcement).

The owner's two hard guardrails ("never update the accounts book", "no discount/price
concession without my confirmation") are enforced at the agent's CAPABILITY boundary, not
merely stored as context. The strongest enforceable invariant on the LLM agent is what tools
it can call: if the agent is never handed a tool that (a) sends to a customer, (b) writes the
owner's accounts book (Google Sheet) / the customer ledger, then it CANNOT take those actions —
every customer send is forced through the campaign approval gate (collapse → request_owner_approval,
Pillar-7), and the Sheets connector is read-only (`spreadsheets.readonly`, no write method).

This module is the runtime fail-CLOSED backstop: `assert_agent_tools_safe` is called at graph
build for every agent surface (orchestrator + integration + any extra_tools). If a future change
ever hands the agent a send-to-customer / ledger-write / Sheets-write tool, the graph build RAISES
rather than silently opening the boundary. The companion test
(`tests/agent/test_no_write_tool_surface.py`) pins the exact allowlist so any tool addition also
trips CI review.

NOT a discount-detection mechanism: concession DETECTION (owner-set terms) is a later piece
(VT-195 ph3 interface). VT-268 locks the boundary so it stays closed until that lands.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)


class ToolGuardrailViolation(RuntimeError):
    """Raised when an agent tool surface exposes a forbidden write/send capability."""


# Capability fragments that MUST NOT appear in any agent-callable tool name. Each names a
# concrete dangerous capability the owner guardrails forbid the agent from holding directly:
#   - customer sends: must route through the campaign approval gate, never a direct tool.
#   - accounts-book / ledger writes: the agent must never write the owner's Sheet or the ledger.
# Deliberately SPECIFIC (not a bare "write") so benign tools — write_l0_fragment (L0 memory),
# compose_owner_output_tool (composes, does not send) — are NOT false-flagged.
FORBIDDEN_CAPABILITY_SUBSTRINGS: tuple[str, ...] = (
    # direct customer-send capabilities (must go via the approval-gated campaign path)
    "send_whatsapp_message",
    "send_whatsapp_template",
    "send_template_message",
    "send_freeform",
    "send_to_customer",
    # accounts-book (owner Google Sheet) writes
    "sheet_append",
    "append_to_sheet",
    "write_sheet",
    "sheet_update",
    "update_sheet",
    "batch_update",
    "values_append",
    "push_to_sheet",
    "write_accounts",
    "accounts_book_write",
    # customer-ledger writes
    "record_ledger",
    "write_ledger",
    "ledger_entr",
    "insert_ledger",
)


def _tool_name(tool: Any) -> str:
    """Best-effort tool name (langchain BaseTool .name; fall back to repr)."""
    name = getattr(tool, "name", None)
    if isinstance(name, str) and name:
        return name
    return type(tool).__name__


def find_forbidden_tools(tools: Iterable[Any]) -> list[tuple[str, str]]:
    """Return [(tool_name, matched_substring)] for tools exposing a forbidden capability."""
    hits: list[tuple[str, str]] = []
    for tool in tools:
        name = _tool_name(tool).lower()
        for sub in FORBIDDEN_CAPABILITY_SUBSTRINGS:
            if sub in name:
                hits.append((_tool_name(tool), sub))
                break
    return hits


def assert_agent_tools_safe(tools: Iterable[Any], *, surface: str) -> None:
    """Fail-CLOSED: raise if any tool exposes a forbidden write/send capability.

    Called at graph build for each agent surface. A no-op for the current safe tool set;
    it only ever raises when a dangerous tool is added — which is exactly the boundary the
    owner guardrails (CL-268 intent) forbid the agent from crossing.
    """
    tools = list(tools)
    hits = find_forbidden_tools(tools)
    if hits:
        detail = ", ".join(f"{n} (matched {s!r})" for n, s in hits)
        raise ToolGuardrailViolation(
            f"agent surface {surface!r} exposes forbidden capability tool(s): {detail}. "
            "Customer sends MUST route through the campaign approval gate (Pillar-7); the agent "
            "MUST NOT hold a direct send / accounts-book-write / ledger-write tool (VT-268)."
        )
    logger.debug("tool guardrail OK: surface=%s tools=%d", surface, len(tools))


__all__ = [
    "ToolGuardrailViolation",
    "FORBIDDEN_CAPABILITY_SUBSTRINGS",
    "find_forbidden_tools",
    "assert_agent_tools_safe",
]
