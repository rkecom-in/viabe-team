"""VT-39 tool-registry tests."""

from __future__ import annotations


import pytest

pytest.importorskip("pydantic")

from pydantic import BaseModel  # noqa: E402

from team_shared.mcp import MCPTool, ToolContext  # noqa: E402

from orchestrator.agent import tool_registry  # noqa: E402


class _NoopInput(BaseModel):
    pass


class _NoopOutput(BaseModel):
    ok: bool = True


class _NoopToolA(MCPTool[_NoopInput, _NoopOutput]):
    name = "noop_a"
    description = "Test-only noop tool A."
    input_schema = _NoopInput
    output_schema = _NoopOutput

    def execute(self, ctx: ToolContext, inputs: _NoopInput) -> _NoopOutput:
        return _NoopOutput()


class _NoopToolB(MCPTool[_NoopInput, _NoopOutput]):
    name = "noop_b"
    description = "Test-only noop tool B."
    input_schema = _NoopInput
    output_schema = _NoopOutput

    def execute(self, ctx: ToolContext, inputs: _NoopInput) -> _NoopOutput:
        return _NoopOutput()


class _NoopLLMBacked(MCPTool[_NoopInput, _NoopOutput]):
    name = "noop_llm"
    description = "Test-only LLM-backed noop tool."
    input_schema = _NoopInput
    output_schema = _NoopOutput

    @classmethod
    def is_llm_backed(cls) -> bool:
        # Rationale: synthetic — the rationale doc tracks real tools.
        return True

    def execute(self, ctx: ToolContext, inputs: _NoopInput) -> _NoopOutput:
        return _NoopOutput()


@pytest.fixture(autouse=True)
def _clean_registry():
    tool_registry._reset_for_tests()
    yield
    tool_registry._reset_for_tests()


def test_register_and_get():
    tool_registry.register(_NoopToolA)
    assert tool_registry.get("noop_a") is _NoopToolA


def test_register_same_class_idempotent():
    tool_registry.register(_NoopToolA)
    tool_registry.register(_NoopToolA)  # no-op
    assert tool_registry.all_tool_names() == ["noop_a"]


def test_register_different_class_under_same_name_raises():
    tool_registry.register(_NoopToolA)
    other_name_a_class = type(
        "Impostor",
        (_NoopToolA,),
        {"name": "noop_a", "description": "Impostor."},
    )
    with pytest.raises(tool_registry.ToolNameCollision):
        tool_registry.register(other_name_a_class)


def test_validate_subset_returns_unknowns():
    tool_registry.register(_NoopToolA)
    missing = tool_registry.validate_subset(["noop_a", "noop_b", "ghost"])
    assert missing == ["noop_b", "ghost"]


def test_llm_backed_in_subset_filters_and_sorts():
    tool_registry.register(_NoopToolA)
    tool_registry.register(_NoopToolB)
    tool_registry.register(_NoopLLMBacked)
    assert tool_registry.llm_backed_in_subset(
        ["noop_a", "noop_llm", "noop_b"]
    ) == ["noop_llm"]


def test_llm_backed_subset_ignores_unknowns():
    tool_registry.register(_NoopLLMBacked)
    assert tool_registry.llm_backed_in_subset(
        ["noop_llm", "ghost"]
    ) == ["noop_llm"]


def test_all_tool_names_is_sorted():
    tool_registry.register(_NoopToolB)
    tool_registry.register(_NoopToolA)
    assert tool_registry.all_tool_names() == ["noop_a", "noop_b"]
