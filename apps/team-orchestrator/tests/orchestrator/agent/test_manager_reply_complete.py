"""VT-591 — unit coverage for ``dispatch._reply_is_incomplete``.

Pure-function tests (no DB, no LLM): the predicate decides whether the manager's
trailing reply needs a compose-completion pass. A True result triggers the ONE
focused no-tools LLM call; a False result skips it (the cost guard — a good reply
pays nothing). We deliberately do NOT exercise the live ``_compose_completed_reply``
LLM call here; only the deterministic gate that fronts it.
"""

from __future__ import annotations

import pytest

# dispatch imports the langchain/langgraph stack at module load.
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")
pytest.importorskip("pydantic")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402


def test_dangling_colon_is_incomplete():
    from orchestrator.agent.dispatch import _reply_is_incomplete

    # The exact truncation VT-591 targets: an explainer intro ending in a colon,
    # emitted as the complete turn with no tool call.
    state = {
        "messages": [
            AIMessage(content="Your data is safe — here's exactly how it works end to end:")
        ]
    }
    assert _reply_is_incomplete(state) is True


def test_dangling_colon_with_trailing_whitespace_is_incomplete():
    from orchestrator.agent.dispatch import _reply_is_incomplete

    # rstrip() before the endswith check — trailing whitespace must not hide the colon.
    state = {"messages": [AIMessage(content="here's how:   \n")]}
    assert _reply_is_incomplete(state) is True


def test_empty_text_is_incomplete():
    from orchestrator.agent.dispatch import _reply_is_incomplete

    state = {"messages": [AIMessage(content="   ")]}
    assert _reply_is_incomplete(state) is True


def test_no_messages_is_incomplete():
    from orchestrator.agent.dispatch import _reply_is_incomplete

    # No transmittable text at all (None from _last_manager_reply_text) → incomplete.
    assert _reply_is_incomplete({}) is True
    assert _reply_is_incomplete({"messages": None}) is True


def test_trailing_toolcall_only_aimessage_is_incomplete():
    from orchestrator.agent.dispatch import _reply_is_incomplete

    # tool_calls with empty content → _last_manager_reply_text returns None → incomplete.
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"name": "classify_owner_message", "args": {}, "id": "c1"}],
            )
        ]
    }
    assert _reply_is_incomplete(state) is True


def test_trailing_aimessage_with_text_and_toolcalls_is_incomplete():
    from orchestrator.agent.dispatch import _reply_is_incomplete

    # Non-empty text that does NOT end in a colon, but the trailing AIMessage still
    # carried tool_calls (it deferred the real content) → complete it.
    state = {
        "messages": [
            AIMessage(
                content="Let me check that for you.",
                tool_calls=[{"name": "lookup", "args": {}, "id": "c2"}],
            )
        ]
    }
    assert _reply_is_incomplete(state) is True


def test_complete_sentence_is_not_incomplete():
    from orchestrator.agent.dispatch import _reply_is_incomplete

    state = {
        "messages": [AIMessage(content="Your GST filing is due on the 20th. You're all set.")]
    }
    assert _reply_is_incomplete(state) is False


def test_message_ending_in_list_item_is_not_incomplete():
    from orchestrator.agent.dispatch import _reply_is_incomplete

    # A full answer that ends on a bulleted list item (not a dangling colon) is complete.
    state = {
        "messages": [
            AIMessage(
                content=(
                    "Here are your two pending tasks:\n"
                    "- File GST by the 20th\n"
                    "- Reply to the Zomato review"
                )
            )
        ]
    }
    assert _reply_is_incomplete(state) is False


def test_complete_reply_before_trailing_toolmessage_is_not_incomplete():
    from orchestrator.agent.dispatch import _reply_is_incomplete

    # Trailing message is a ToolMessage; the AIMessage before it has a complete answer
    # and NO tool_calls → _last_manager_reply_text returns it, and the reverse scan finds
    # that same AIMessage with empty tool_calls → complete.
    state = {
        "messages": [
            HumanMessage(content="what's due?"),
            AIMessage(content="You have one filing due on the 20th."),
            ToolMessage(content="tool output", tool_call_id="c3"),
        ]
    }
    assert _reply_is_incomplete(state) is False
