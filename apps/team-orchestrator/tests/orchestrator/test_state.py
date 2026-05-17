"""VT-3.2 unit tests — SubscriberState shape + the no-agent-import lint rule.

Pure (no DB, no DBOS) — runs in both the lightweight `test` job and the
`orchestrator` job.
"""

from __future__ import annotations

import subprocess
from uuid import uuid4

from orchestrator.state import (
    MAX_TRIAL_EXTENSIONS,
    TERMINAL_PHASES,
    new_subscriber_state,
)

_NOTION_FIELDS = {
    "tenant_id",
    "run_id",
    "phase",
    "phase_entered_at",
    "trial_started_at",
    "trial_extension_count",
    "paid_conversion_at",
    "last_campaign_at",
    "attribution_close_pending",
    "total_arrr_paise",
    "cumulative_fees_paid_paise",
    "escalation_pending",
    "last_owner_message_at",
    "history",
}


def test_subscriber_state_has_exactly_the_notion_fields():
    state = new_subscriber_state(uuid4())
    assert set(state.keys()) == _NOTION_FIELDS


def test_new_subscriber_state_defaults():
    tenant_id = uuid4()
    state = new_subscriber_state(tenant_id)
    assert state["tenant_id"] == tenant_id
    assert state["phase"] == "onboarding"
    assert state["trial_extension_count"] == 0
    assert state["trial_started_at"] is None
    assert state["paid_conversion_at"] is None
    assert state["attribution_close_pending"] == []
    assert state["total_arrr_paise"] == 0
    assert state["cumulative_fees_paid_paise"] == 0
    assert state["escalation_pending"] is False
    assert state["last_owner_message_at"] is None
    assert state["history"] == []


def test_terminal_phases_and_extension_cap():
    assert TERMINAL_PHASES == frozenset({"cancelled", "refunded"})
    assert MAX_TRIAL_EXTENSIONS == 3


def test_agent_code_cannot_import_transitions(tmp_path):
    """The CI grep that bans agent/specialist imports of transitions.py /
    invariants.py fires on a synthetic violation."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "rogue.py").write_text(
        "from orchestrator.transitions import apply_transition\n"
    )
    result = subprocess.run(
        [
            "grep",
            "-rnE",
            r"(import|from) .*\b(transitions|invariants)\b",
            str(agent_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "grep should have flagged the forbidden import"
    assert "transitions" in result.stdout
