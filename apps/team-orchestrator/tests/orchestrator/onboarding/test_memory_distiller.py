"""VT-571 — pure unit tests for the conversation-memory distiller + the window-overflow split.

No DB, no live LLM: the single Haiku call is behind ``memory_distiller._invoke_distill``, monkeypatched
to return canned text, so the prompt-build → parse path is exercised deterministically. The rolling-
window overflow split (``journey._split_overflow``) is a pure function pinned here too.

Import guards: ``memory_distiller`` imports ``dbos`` (the ``@DBOS.workflow()``) and, via
``orchestrator.db``, langgraph + psycopg — so the module (and ``journey``, for the split helper) is
skipped in the dep-less smoke suite and runs in full under the onboarding suite (house importorskip
discipline).
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("langgraph")
pytest.importorskip("dbos")

from orchestrator.onboarding import memory_distiller  # noqa: E402 — after dependency skip guards
from orchestrator.onboarding.journey import _split_overflow  # noqa: E402


# --- the distiller: prompt build + parse (mocked Haiku) -----------------------------------------


def test_distill_returns_stripped_summary_and_prompt_is_well_formed(monkeypatch):
    """One Haiku call folds the evicted transcript + prior summary into a new summary; the returned text
    is stripped. The fold prompt carries the prior summary, the oldest-first transcript, the ≤120-word
    compaction rule, and the PII (no phone/email digits) instruction."""
    captured: dict[str, str] = {}

    def _fake(prompt: str) -> str:
        captured["prompt"] = prompt
        return "  Runs an electronics repair shop in Pune; wants WhatsApp order updates.  "

    monkeypatch.setattr(memory_distiller, "_invoke_distill", _fake)
    evicted = [
        {"role": "owner", "text": "we fix phones and laptops"},
        {"role": "bot", "text": "Great — where are you based?"},
        {"role": "owner", "text": "Pune"},
    ]
    out = memory_distiller.distill_evicted_turns("t1", evicted, prior_summary="Prior facts.")
    assert out == "Runs an electronics repair shop in Pune; wants WhatsApp order updates."

    p = captured["prompt"]
    assert "Prior facts." in p, "the prior summary must be folded in"
    assert "OWNER: we fix phones and laptops" in p, "owner turns rendered oldest-first"
    assert "ASSISTANT: Great — where are you based?" in p, "bot turns rendered as ASSISTANT"
    assert "120 words" in p, "the ≤120-word compaction rule must be present"
    assert "phone numbers" in p, "the PII (no contact digits) instruction must be present"


def test_distill_failure_returns_none(monkeypatch):
    """A raising Haiku call → None (fail-soft; the workflow then leaves the prior summary untouched)."""
    def _boom(prompt: str) -> str:
        raise RuntimeError("haiku down")

    monkeypatch.setattr(memory_distiller, "_invoke_distill", _boom)
    assert memory_distiller.distill_evicted_turns("t1", [{"role": "owner", "text": "x"}], None) is None


def test_distill_empty_output_returns_none(monkeypatch):
    """A blank/whitespace model output → None (nothing usable → keep the prior summary)."""
    monkeypatch.setattr(memory_distiller, "_invoke_distill", lambda p: "   ")
    assert memory_distiller.distill_evicted_turns("t1", [{"role": "owner", "text": "x"}], None) is None


def test_distill_no_durable_turns_skips_llm(monkeypatch):
    """All-empty evicted turns → no LLM call at all, returns None (nothing to fold)."""
    called: list[int] = []
    monkeypatch.setattr(memory_distiller, "_invoke_distill", lambda p: called.append(1) or "unused")
    out = memory_distiller.distill_evicted_turns(
        "t1", [{"role": "owner", "text": ""}, {"role": "bot", "text": "   "}], "prior"
    )
    assert out is None
    assert not called, "an empty evicted set must never call the LLM"


def test_distill_prior_none_renders_placeholder(monkeypatch):
    """No prior summary yet → the prompt renders the '(none yet)' placeholder, not a bare blank."""
    captured: dict[str, str] = {}
    monkeypatch.setattr(memory_distiller, "_invoke_distill", lambda p: captured.setdefault("p", p) or "s")
    memory_distiller.distill_evicted_turns("t1", [{"role": "owner", "text": "a"}], None)
    assert "(none yet)" in captured["p"]


# --- the rolling-window overflow split (pure) ---------------------------------------------------


def test_split_no_overflow_keeps_all_evicts_nothing():
    existing = [{"role": "owner", "text": f"o{i}"} for i in range(3)]
    cleaned = [{"role": "bot", "text": "b"}]
    kept, evicted = _split_overflow(existing, cleaned, 8)
    assert kept == existing + cleaned
    assert evicted == []


def test_split_at_exact_cap_evicts_nothing():
    combined = [{"role": "owner", "text": f"o{i}"} for i in range(8)]
    kept, evicted = _split_overflow(combined, [], 8)
    assert len(kept) == 8 and evicted == []


def test_split_overflow_evicts_oldest_head_keeps_newest_tail():
    """Nine entries into a cap-8 window: the oldest one is EVICTED (returned for distillation, not
    dropped) and the newest 8 are KEPT in order."""
    existing = [{"role": "owner", "text": f"o{i}"} for i in range(7)]  # 7
    cleaned = [{"role": "bot", "text": "b0"}, {"role": "owner", "text": "o7"}]  # +2 → 9
    kept, evicted = _split_overflow(existing, cleaned, 8)
    assert len(kept) == 8
    assert len(evicted) == 1
    assert evicted[0]["text"] == "o0", "the oldest entry is the one evicted"
    assert kept[0]["text"] == "o1", "oldest surviving entry"
    assert kept[-1]["text"] == "o7", "newest entry last, order preserved"
