"""VT-463 regression — _extract_user_request_from_state must find the user message
even when dispatch prepends SystemMessage blocks (L1 / business-context / manager-intent).

The live re-drive caught the spawn crashing: dispatch.py inserts SystemMessage(s) at
index 0, so messages[0] is a SystemMessage, and the old code indexed [0] and raised
"state['messages'][0] is not a user message" — stranding the win-back spawn. The fix
scans for the FIRST HumanMessage instead of indexing [0].
"""

from __future__ import annotations

import pytest

# langchain_core + the handoffs module (which imports langgraph/langchain) are absent in the
# lean CI ``test`` job and the pre-push dep-less smoke — skip there so collection never breaks
# (the full ``orchestrator`` job, deps present, runs these). Mirrors the dep-less-smoke discipline.
pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from orchestrator.handoffs import _extract_user_request_from_state  # noqa: E402


def test_user_request_found_after_prepended_system_blocks():
    # The live dispatch order: SystemMessage blocks prepended ahead of the owner message.
    state = {
        "messages": [
            SystemMessage(content="## L1 context\n…"),
            SystemMessage(content="## Business context\n…"),
            SystemMessage(content="## Manager intent signal\n…"),
            HumanMessage(content="find my lapsed customers and win them back"),
        ]
    }
    assert (
        _extract_user_request_from_state(state)
        == "find my lapsed customers and win them back"
    )


def test_user_request_humanmessage_at_index_0_still_works():
    state = {"messages": [HumanMessage(content="send a win-back")]}
    assert _extract_user_request_from_state(state) == "send a win-back"


def test_user_request_dict_role_user_shape():
    state = {
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "win-back please"},
        ]
    }
    assert _extract_user_request_from_state(state) == "win-back please"


def test_user_request_list_content_blocks_joined():
    state = {
        "messages": [
            SystemMessage(content="sys"),
            HumanMessage(content=[{"type": "text", "text": "win "}, {"type": "text", "text": "back"}]),
        ]
    }
    assert _extract_user_request_from_state(state) == "win back"


def test_no_user_message_raises():
    state = {"messages": [SystemMessage(content="only system")]}
    with pytest.raises(ValueError, match="no user message"):
        _extract_user_request_from_state(state)


def test_empty_messages_raises():
    with pytest.raises(ValueError, match="empty"):
        _extract_user_request_from_state({"messages": []})
