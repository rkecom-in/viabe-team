"""VT-195 Phase 2 — structural cache-safety of the L1 system-block injection.

D2 requires the per-tenant L1 block to land as a SEPARATE system block AFTER the
VT-194 cached prefix — never merged INTO the cached block (that would change the
cached prefix bytes and defeat the 90% cache discount).

This test proves the STRUCTURE without a live Anthropic call: it runs the same
langchain_anthropic message→request conversion the model uses and asserts the
`system` param is two distinct blocks — the VT-194 cached prefix FIRST and
byte-identical (cache_control intact), the L1 block SECOND and uncached. The
live cache_read assertion (binding) is the keyed canary vt195_l1_phase2_dispatch.

No DB, no API key.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_anthropic")

from langchain_anthropic.chat_models import _format_messages  # noqa: E402
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402


def test_l1_block_is_separate_system_block_after_cached_prefix() -> None:
    from orchestrator.agent.orchestrator_agent import ORCHESTRATOR_AGENT_SYSTEM_MESSAGE

    l1 = SystemMessage(content="# Tenant context (L1)\n- Business archetype: electronics_retail")
    # create_agent supplies the cached system_prompt first; dispatch prepends the
    # L1 SystemMessage to the input messages → this is the effective order.
    system, _formatted = _format_messages(
        [ORCHESTRATOR_AGENT_SYSTEM_MESSAGE, l1, HumanMessage(content="hi")]
    )

    assert isinstance(system, list)
    assert len(system) == 2, f"expected 2 system blocks (cached prefix + L1), got {system}"

    cached, l1_block = system[0], system[1]
    # 1) The VT-194 cached prefix is FIRST and still carries cache_control.
    assert cached.get("cache_control") == {"type": "ephemeral"}
    # 2) Its text is the unmodified orchestrator system prompt (cache prefix
    #    bytes unchanged → cache still HITs).
    from orchestrator.agent.orchestrator_agent import ORCHESTRATOR_AGENT_SYSTEM_PROMPT

    assert cached["text"] == ORCHESTRATOR_AGENT_SYSTEM_PROMPT
    # 3) The L1 block is SECOND and UNCACHED (a per-tenant block must never carry
    #    cache_control — that would fragment the shared cache).
    assert "cache_control" not in l1_block
    assert "Tenant context (L1)" in l1_block["text"]
