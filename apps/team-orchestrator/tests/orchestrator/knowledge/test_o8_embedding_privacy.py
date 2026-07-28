"""VT-709 — O8 text is PII-redacted before it reaches the embedding transport."""

from __future__ import annotations

import pytest

# Importing the legacy ``orchestrator.knowledge`` package currently initializes its database-backed
# L1 exports.  Keep the dependency-less smoke faithful: this focused test runs in the project/DB
# jobs where psycopg is present, while the O8 contracts themselves remain dependency-free.
pytest.importorskip("psycopg")

from orchestrator.knowledge import embeddings


def test_redact_for_embedding_removes_indian_business_identifiers_and_contact_data() -> None:
    safe = embeddings.redact_for_embedding(
        [
            "GSTIN 27AAKCD4875D1ZG, PAN AAKCD4875D, email owner@example.com, "
            "phone +919876543210"
        ]
    )[0]
    assert "27AAKCD4875D1ZG" not in safe
    assert "AAKCD4875D" not in safe
    assert "owner@example.com" not in safe
    assert "+919876543210" not in safe
    assert "<gst:redacted>" in safe
    assert "<pan:redacted>" in safe


def test_embedding_wrapper_calls_redactor_before_transport(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_embed(texts, *, input_type=None):  # noqa: ANN001, ANN202
        observed["texts"] = texts
        observed["input_type"] = input_type
        return [[0.0] * embeddings.EMBED_DIM]

    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
    result = embeddings.embed_redacted_texts(
        ["Contact owner@example.com about PAN AAKCD4875D"], input_type="document"
    )
    sent = observed["texts"][0]
    assert "owner@example.com" not in sent
    assert "AAKCD4875D" not in sent
    assert observed["input_type"] == "document"
    assert len(result[0]) == embeddings.EMBED_DIM


def test_embedding_redaction_preserves_long_body_semantics() -> None:
    text = "Operational cadence and cash collection discipline. " * 20
    safe = embeddings.redact_for_embedding([text])[0]
    assert "Operational cadence" in safe
    assert not safe.startswith("<body:hash:")
