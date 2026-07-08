"""VT-628 — the temperature family-gate: opus omits the param (400s otherwise),
everything else pins to 0.0 for determinism."""

from orchestrator.llm_config import sampling_kwargs


def test_opus_omits_temperature() -> None:
    # opus-4-7 AND opus-4-8 both reject the temperature param — must be absent.
    assert sampling_kwargs("claude-opus-4-8") == {}
    assert sampling_kwargs("claude-opus-4-7") == {}
    assert sampling_kwargs("CLAUDE-OPUS-4-8") == {}  # case-insensitive


def test_non_opus_pins_zero() -> None:
    assert sampling_kwargs("claude-sonnet-5") == {"temperature": 0.0}
    assert sampling_kwargs("claude-haiku-4-5") == {"temperature": 0.0}
    assert sampling_kwargs("claude-haiku-4-5-20251001") == {"temperature": 0.0}
