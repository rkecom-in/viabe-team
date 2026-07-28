"""Static custody gate for the egress-dependent VT-710 source canary."""

from __future__ import annotations

from pathlib import Path

CANARY = Path(__file__).resolve().parents[3] / "canaries" / "vt710_o8_source_canary.py"


def test_canary_has_three_source_classes_and_fail_not_skip_embedding() -> None:
    text = CANARY.read_text(encoding="utf-8")
    assert "sba.gov" in text
    assert "nber.org" in text
    assert "AdversarialForumExtractor" in text
    assert "embed_redacted_texts" in text
    assert "EMBED_DIM" in text
    assert "pytest.skip" not in text
    assert "except Exception" not in text
    assert "live_writes\": 0" in text


def test_canary_never_embeds_fetched_third_party_bodies() -> None:
    text = CANARY.read_text(encoding="utf-8")
    assert "probes = [" in text
    assert "official_hash[:16]" in text
    assert "research_hash[:16]" in text
    assert "embed_redacted_texts(probes" in text
    assert "embed_redacted_texts(official" not in text
    assert "embed_redacted_texts(research" not in text
