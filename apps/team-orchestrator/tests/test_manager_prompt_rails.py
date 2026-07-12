"""DF10 — guard the team-manager brain-prompt honesty rails from silent regression.

The team-manager system prompt (src/orchestrator/prompts/orchestrator_agent_system.md)
carries three prompt-mediated honesty rails plus a mandatory re-scope of the autonomy
claim. These are behavioural, not deterministic — nothing in code enforces them, so a
careless prompt edit could drop or contradict them without any other test failing.
This test is the forcing function:

  (a) the UNSCOPED "without per-action owner approval" claim about ALL actions is GONE
      (a scoped "... for analysis/planning/drafting" variant is allowed) — else the
      prompt self-contradicts the money carve-out;
  (b) the MONEY carve-out (never spend/send on own authority; draft → owner approves) is present;
  (c) the OWN-DATA anti-fabrication rail (never claim you can't see the owner's own data) is present;
  (d) the STOP/PAUSE speech-act rail (acknowledge, never a customer lookup) is present.

Dep-less on purpose (only pathlib + re + str) so it runs in the pre-push dep-less smoke
+ the CI `test` job. Editing these rails is a deliberate act — update this test in the
same PR (it is the forcing function).
"""

from __future__ import annotations

import re
from pathlib import Path


def _prompt_path() -> Path:
    # tests/ -> team-orchestrator ; then src/orchestrator/prompts/…
    return (
        Path(__file__).resolve().parent.parent
        / "src"
        / "orchestrator"
        / "prompts"
        / "orchestrator_agent_system.md"
    )


def _prompt_text() -> str:
    """Prompt text with all whitespace runs collapsed to single spaces, so anchor
    substrings match regardless of the source file's line-wrapping."""
    return " ".join(_prompt_path().read_text(encoding="utf-8").split())


def test_prompt_file_exists() -> None:
    assert _prompt_path().is_file(), f"brain prompt not found at {_prompt_path()}"


def test_unscoped_autonomy_claim_is_rescoped() -> None:
    """(a) The bare "without per-action owner approval" claim (covering ALL actions) must
    be gone. A scoped variant ("... for analysis/planning/drafting") is fine — the guard
    only bans the unscoped form that contradicts the money carve-out."""
    text = _prompt_text()
    # Match the phrase only when NOT immediately followed by a scoping " for ...".
    unscoped = re.findall(r"without per-action owner approval(?! for )", text)
    assert not unscoped, (
        "The unscoped 'without per-action owner approval' claim is still present — it "
        "contradicts the money carve-out (effectful sends/spends DO need owner approval). "
        "Re-scope it to non-effectful work (analysis/planning/drafting) or reword it out."
    )


def test_money_carveout_present() -> None:
    """(b) Money carve-out: brain never spends/sends on its own authority; draft → owner approves."""
    text = _prompt_text()
    assert "never spend money or send to a customer on your own" in text, (
        "Money carve-out missing: the prompt must state the brain never spends/sends on its own authority."
    )
    assert "no — I draft it and you approve" in text, (
        "Money carve-out missing the honest 'can you spend/send without asking me?' -> NO answer."
    )
    assert "never state a specific customer rupee figure" in text, (
        "Money carve-out missing the anti-fabrication of a specific customer ₹ figure."
    )


def test_own_data_anti_fabrication_present() -> None:
    """(c) Own-data rail: never invent a false NEGATIVE capability about the owner's own data."""
    text = _prompt_text()
    assert "never claim an inability to see their own data" in text, (
        "Own-data anti-fabrication rail missing: the brain must not claim it cannot see the owner's own data."
    )
    assert "anonymized IDs" in text, (
        "Own-data rail should name the concrete false claim ('I only see anonymized IDs') it forbids."
    )


def test_stop_speech_act_present() -> None:
    """(d) Stop/pause is a speech-act to ACKNOWLEDGE (global vs per-customer), never a customer lookup."""
    text = _prompt_text()
    assert "is a control you ACKNOWLEDGE" in text, (
        "Stop/pause speech-act rail missing: a stop/pause turn must be acknowledged, not researched."
    )
    assert 'NEVER reply "I couldn\'t find that customer"' in text, (
        "Stop/pause rail must forbid the 'I couldn't find that customer' lookup response."
    )
