"""VT-628 — the temperature family-gate. On the current model lineup ONLY haiku
accepts the temperature param; sonnet-5 AND opus-4-7/4-8 DEPRECATE it (400). So
the gate pins temp=0 for haiku and omits it for everything else."""

from orchestrator.llm_config import sampling_kwargs


def test_haiku_pins_zero() -> None:
    assert sampling_kwargs("claude-haiku-4-5") == {"temperature": 0.0}
    assert sampling_kwargs("claude-haiku-4-5-20251001") == {"temperature": 0.0}
    assert sampling_kwargs("CLAUDE-HAIKU-4-5") == {"temperature": 0.0}  # case-insensitive


def test_sonnet_and_opus_omit_temperature() -> None:
    # Both DEPRECATE temperature (400) — the param MUST be absent or the call fails.
    assert sampling_kwargs("claude-sonnet-5") == {}
    assert sampling_kwargs("claude-opus-4-8") == {}
    assert sampling_kwargs("claude-opus-4-7") == {}
