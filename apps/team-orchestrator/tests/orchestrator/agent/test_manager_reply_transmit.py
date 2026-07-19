"""VT-589 — unit coverage for ``dispatch._last_manager_reply_text``.

Pure-function tests (no DB, no LLM): the helper extracts the manager's OWN final
answer from the supervisor terminal state so a no-spawn "handle-directly" turn
transmits the real reply instead of runner.py's generic D1 fallback. Verifies the
reverse scan stops at the trailing AIMessage (never digs into stale earlier
reasoning), both content shapes (plain str + list-of-blocks), and the empty/absent
cases that must yield None.
"""

from __future__ import annotations

import pytest

# dispatch imports the langchain/langgraph stack at module load.
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")
pytest.importorskip("pydantic")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402


def test_plain_str_content_returned_stripped():
    from orchestrator.agent.dispatch import _last_manager_reply_text

    state = {"messages": [AIMessage(content="  Your GST filing is due on the 20th.  ")]}
    assert _last_manager_reply_text(state) == "Your GST filing is due on the 20th."


def test_list_of_blocks_content_joined():
    from orchestrator.agent.dispatch import _last_manager_reply_text

    state = {
        "messages": [
            AIMessage(
                content=[
                    {"type": "text", "text": "Here is "},
                    {"type": "text", "text": "your answer."},
                ]
            )
        ]
    }
    assert _last_manager_reply_text(state) == "Here is your answer."


def test_trailing_toolmessage_falls_back_to_prior_aimessage_text():
    from orchestrator.agent.dispatch import _last_manager_reply_text

    # Last message is a ToolMessage; the AIMessage-with-text just before it is the
    # manager's answer → skip the tool result, return the AIMessage text.
    state = {
        "messages": [
            HumanMessage(content="what's due?"),
            AIMessage(content="You have one filing due."),
            ToolMessage(content="tool output", tool_call_id="call_1"),
        ]
    }
    assert _last_manager_reply_text(state) == "You have one filing due."


def test_empty_whitespace_aimessage_returns_none():
    from orchestrator.agent.dispatch import _last_manager_reply_text

    state = {"messages": [AIMessage(content="   ")]}
    assert _last_manager_reply_text(state) is None


def test_trailing_toolcall_only_aimessage_returns_none():
    from orchestrator.agent.dispatch import _last_manager_reply_text

    # The trailing AIMessage carries only tool_calls with empty content — do NOT
    # dig past it into the earlier (stale) reasoning turn; return None.
    tool_call_msg = AIMessage(
        content="",
        tool_calls=[
            {"name": "classify_owner_message", "args": {}, "id": "call_9"}
        ],
    )
    state = {
        "messages": [
            AIMessage(content="stale earlier reasoning — must NOT be returned"),
            tool_call_msg,
        ]
    }
    assert _last_manager_reply_text(state) is None


def test_no_aimessage_returns_none():
    from orchestrator.agent.dispatch import _last_manager_reply_text

    state = {
        "messages": [
            HumanMessage(content="hello"),
            ToolMessage(content="tool output", tool_call_id="call_2"),
        ]
    }
    assert _last_manager_reply_text(state) is None


def test_missing_messages_key_returns_none():
    from orchestrator.agent.dispatch import _last_manager_reply_text

    assert _last_manager_reply_text({}) is None
    assert _last_manager_reply_text({"messages": None}) is None
