"""VT-709 — knowledge incident free text is redacted before wrapper persistence."""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.db.base import TenantScopedTable  # noqa: E402
from orchestrator.db.wrappers import KnowledgeIncidentsWrapper  # noqa: E402


def test_incident_writer_redacts_both_free_text_fields_before_insert(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_insert(self, tenant_id, payload, *, conn=None):  # noqa: ANN001, ANN202
        captured["tenant_id"] = tenant_id
        captured["payload"] = payload
        captured["conn"] = conn
        return {"id": str(uuid4()), "tenant_id": str(tenant_id), **payload}

    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "vt709-test-salt")
    monkeypatch.setattr(TenantScopedTable, "insert", fake_insert)
    tenant_id, card_ref, evidence_ref = uuid4(), uuid4(), uuid4()
    row = KnowledgeIncidentsWrapper().record_redacted(
        tenant_id,
        incident_class="regulatory",
        card_version_ref=card_ref,
        evidence_refs=[evidence_ref],
        detail="GSTIN 27AAKCD4875D1ZG; owner owner@example.com; +919876543210",
        resolution="Removed PAN AAKCD4875D from the derived card.",
    )

    payload = captured["payload"]
    rendered = repr(payload)
    for forbidden in (
        "27AAKCD4875D1ZG",
        "owner@example.com",
        "+919876543210",
        "AAKCD4875D",
    ):
        assert forbidden not in rendered
    assert payload["detail_redacted"]
    assert payload["resolution_redacted"]
    assert row["card_version_ref"] == str(card_ref)
    assert row["evidence_refs"] == [str(evidence_ref)]
