"""Repair orphaned ``tool_use`` blocks before a lane's Anthropic model call (VT-622).

THE DEFECT: a dual/parallel spawn — the orchestrator emits TWO handoff ``tool_use``
blocks in one assistant turn (e.g. ``spawn_sales_recovery`` + ``spawn_integration`` for a
dual-intent owner message) — routes away via the FIRST ``Command(goto=...)`` before the
SECOND ``tool_use`` receives its ``ToolMessage``. The target lane's ``create_agent`` then
replays the parent history carrying an UNPAIRED ``tool_use`` → the next Anthropic call
returns ``400 invalid_request_error`` (``tool_use ids were found without tool_result
blocks``) → the lane raises ``BadRequestError`` → VT-602 catches it and the whole turn
degrades to a human ``escalated`` (observed: ``routing_dual_intent_connect_and_winback``).

This is the MESSAGE-LEVEL analogue of VT-484's ``_tool_error_to_tool_result``: VT-484
covers tools that RAISE, but spawn handoffs RETURN a ``Command`` (never raise), so VT-484
does not fire. Same failure mode (orphaned ``tool_use`` → 400), different trigger.

THE FIX is Anthropic-safety ONLY: for every ``tool_use`` id with no following
``ToolMessage``, insert a synthetic error ``tool_result`` immediately after its
``AIMessage``. A conversation that is ALREADY valid is returned UNCHANGED (identity), so
wiring ``repair_tool_pairs_before_model`` into a lane's ``create_agent(middleware=...)``
can never degrade a healthy turn — it only rescues one that would otherwise 400. The
repair edits the MODEL INPUT (``request.override(messages=...)``) only; it does not mutate
persisted graph state.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import wrap_model_call
from langchain_core.messages import AIMessage, ToolMessage

logger = logging.getLogger(__name__)

_SUPERSEDED = (
    "[tool call superseded — the run routed to another lane before this call executed; "
    "no result was produced]"
)


def repair_orphaned_tool_use(messages: list[Any]) -> list[Any]:
    """Return ``messages`` with a synthetic error ``tool_result`` inserted for any
    ``tool_use`` id that has no following ``ToolMessage``.

    Identity (returns the SAME list object) when every ``tool_use`` is already paired —
    the healthy path is a strict no-op. The synthetic ``ToolMessage`` is inserted directly
    after the ``AIMessage`` that made the unpaired call, keeping the ``tool_use`` /
    ``tool_result`` blocks Anthropic-valid (all results in the immediately-following user
    turn; result order within it is unconstrained).
    """
    resolved: set[str] = {
        m.tool_call_id
        for m in messages
        if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None)
    }

    def _has_unpaired(m: Any) -> bool:
        return isinstance(m, AIMessage) and any(
            tc.get("id") not in resolved for tc in (m.tool_calls or [])
        )

    if not any(_has_unpaired(m) for m in messages):
        return messages  # already Anthropic-valid — identity no-op

    out: list[Any] = []
    for m in messages:
        out.append(m)
        if isinstance(m, AIMessage) and m.tool_calls:
            for tc in m.tool_calls:
                tcid = tc.get("id")
                if tcid and tcid not in resolved:
                    out.append(
                        ToolMessage(
                            content=_SUPERSEDED,
                            tool_call_id=tcid,
                            name=tc.get("name") or "",
                            status="error",
                        )
                    )
                    resolved.add(tcid)
    logger.warning(
        "tool_pairing: repaired %d orphaned tool_use block(s) before model call "
        "(VT-622 — preventing a dual-spawn 400)",
        len(out) - len(messages),
    )
    return out


@wrap_model_call
def repair_tool_pairs_before_model(request: Any, handler: Any) -> Any:
    """Lane middleware: repair orphaned ``tool_use`` in the model input, then proceed.

    Wire into a lane's ``create_agent(middleware=[repair_tool_pairs_before_model, ...])``.
    Safe on every turn (identity when the conversation is already valid).
    """
    fixed = repair_orphaned_tool_use(request.messages)
    if fixed is not request.messages:
        request = request.override(messages=fixed)
    return handler(request)
