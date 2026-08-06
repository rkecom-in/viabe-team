"""VT-727 routes-out: evidence-bound resolution of every non-T4 deferral."""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pydantic")

from orchestrator.knowledge.contracts import SourceClass
from orchestrator.knowledge.persisted_embeddings import card_content_digest
from orchestrator.knowledge.registry_full import build_full_plan, load_independence_audit
from orchestrator.knowledge.registry_resolution import (
    ResolutionError,
    build_resolution_plan,
    copy_resolution_embeddings,
    load_resolution_delta,
    persist_resolution_plan,
)

ROOT = Path(__file__).resolve().parents[5]
CORPUS = ROOT / "apps" / "team-orchestrator" / "knowledge_corpus"


def _jsonl(name: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (CORPUS / name).read_text(encoding="utf-8").splitlines()
        if line
    ]


def _full_plan():
    audit = load_independence_audit(
        json.loads((CORPUS / "independence_audit.json").read_text(encoding="utf-8"))
    )
    return build_full_plan(_jsonl("source_rights.jsonl"), _jsonl("candidate_cards.jsonl"), audit)


def _resolution_rows():
    return load_resolution_delta(_jsonl("deferral_resolution_delta.jsonl"))


def test_delta_resolves_exactly_25_t1_9_t3_and_2_t1v_rows() -> None:
    rows = _resolution_rows()
    assert len(rows) == 36
    assert Counter(row.source_class for row in rows) == {"t1": 25, "t3": 9, "t1v": 2}
    assert Counter(reason for row in rows for reason in row.cleared_reasons) == {
        "authoritative_effective_date_unverified": 25,
        "originality_attestation_requires_independent_recheck": 12,
        "vendor_policy_currentness_requires_review": 2,
    }
    assert sum(row.originality is not None for row in rows if row.source_class == "t3") == 9
    assert all(row.authorizes_effects is False for row in rows)


def test_v3_shadow_snapshot_has_100_eligible_and_leaves_all_18_t4_untouched() -> None:
    full = _full_plan()
    plan = build_resolution_plan(full, _resolution_rows())
    assert len(plan.members) == 118
    assert len(plan.promotions) == 36
    assert plan.shadow_validated_count == 100
    assert plan.deferred_count == 18
    assert plan.AUTHORIZES_EFFECTS is False
    assert plan.corpus_status == "shadow"
    assert plan.admission_verdict == "pending"

    original_t4 = {
        item.legacy_id: item.representative
        for item in full.cards
        if item.source_class is SourceClass.T4_EXPERIENTIAL
    }
    final_by_card_key = {card.card_id: card for card in plan.members}
    assert len(original_t4) == 18
    for card in original_t4.values():
        assert final_by_card_key[card.card_id] == card
        assert final_by_card_key[card.card_id].retrieval_eligible is False


def test_evidence_is_exact_complete_and_cannot_smuggle_card_expression() -> None:
    raw = _jsonl("deferral_resolution_delta.jsonl")
    forbidden = {"claim", "distillation_note", "claim_value", "raw_text", "card"}
    assert all(forbidden.isdisjoint(row) for row in raw)
    assert all(row["prior_disposition"] == "deferred_candidate" for row in raw)
    assert all(row["resolved_disposition"] == "shadow_validated" for row in raw)

    missing = raw[:-1]
    with pytest.raises(ResolutionError, match="expected 36"):
        load_resolution_delta(missing)

    injected = deepcopy(raw)
    injected[0]["claim"] = "unreviewed replacement expression"
    with pytest.raises(ResolutionError, match="Extra inputs"):
        load_resolution_delta(injected)

    wrong_reason = load_resolution_delta(raw)
    tampered = list(wrong_reason)
    tampered[0] = tampered[0].model_copy(update={"cleared_reasons": ("made_up_gate",)})
    with pytest.raises(ResolutionError, match="every prior deferral reason"):
        build_resolution_plan(_full_plan(), tampered)


def test_dates_replace_observation_dates_and_record_normalization() -> None:
    full = _full_plan()
    rows = _resolution_rows()
    plan = build_resolution_plan(full, rows)
    candidate_by_id = {
        item.legacy_id: item.candidate
        for item in full.cards
        if item.disposition == "deferred_candidate"
    }
    promoted_by_id = {item.legacy_id: item.validated for item in plan.promotions}
    dated = [row for row in rows if row.effective_period is not None]
    assert len(dated) == 25
    assert sum(row.effective_period.mode == "primary" for row in dated) == 24
    assert sum(row.effective_period.mode == "secondary_attested" for row in dated) == 1
    assert (
        next(
            row
            for row in dated
            if row.legacy_id == "bk057-independent-red-team-assumption-challenge"
        ).effective_period.mode
        == "secondary_attested"
    )
    assert (
        next(
            row
            for row in dated
            if row.legacy_id == "bk059-crisis-incident-command-operating-periods"
        ).effective_period.mode
        == "primary"
    )
    assert all(row.effective_period.locator for row in dated if row.effective_period)
    assert all(row.effective_period.source_date_text for row in dated if row.effective_period)
    for row in dated:
        assert row.effective_period is not None
        before = candidate_by_id[row.legacy_id].applicability
        after = promoted_by_id[row.legacy_id].applicability
        assert after.effective_from == row.effective_period.effective_from
        assert after.effective_to == row.effective_period.effective_to
        assert after.effective_from != before.effective_from

    survey = next(row for row in dated if row.legacy_id.startswith("bk109-"))
    assert survey.effective_period is not None
    assert survey.effective_period.effective_from.isoformat() == "2021-12-01T00:00:00+00:00"
    assert survey.effective_period.effective_to is not None
    assert survey.effective_period.effective_to.isoformat() == "2022-03-31T23:59:59+00:00"


def test_originality_resolution_is_measured_or_attributably_retained() -> None:
    evidence = [row.originality for row in _resolution_rows() if row.originality is not None]
    assert len(evidence) == 12
    assert Counter(item.mode for item in evidence) == {
        "mechanical": 8,
        "attestation_stands": 4,
    }
    for item in evidence:
        if item.mode == "mechanical":
            assert item.scanner == "token-shingle-v1"
            assert item.outcome == "pass"
            assert item.source_sha256 is not None and len(item.source_sha256) == 64
            assert item.attested_by is None
        else:
            assert item.scanner is None
            assert item.source_sha256 is None
            assert item.attested_by
            assert "live_link" in item.rationale_code
            assert "rkecom_synthesis" in item.rationale_code
            assert "not_archived" in item.rationale_code or "not_committed" in item.rationale_code


def test_platform_policy_pass_is_first_party_current_and_date_honest() -> None:
    rows = {row.legacy_id: row for row in _resolution_rows() if row.vendor_policy is not None}
    assert len(rows) == 2
    whatsapp = rows["bk011-whatsapp-business-policy-send-discipline"].vendor_policy
    google = rows["bk012-google-business-profile-representation-discipline"].vendor_policy
    assert whatsapp is not None and google is not None
    assert whatsapp.publisher == "WhatsApp for Business"
    assert whatsapp.effective_date_status == "published"
    assert whatsapp.effective_from is not None
    assert len(whatsapp.support_locators) >= 2
    assert google.publisher == "Google"
    assert google.effective_date_status == "not_published_by_vendor"
    assert google.effective_from is None
    assert len(google.support_locators) >= 2

    plan = build_resolution_plan(
        _full_plan(),
        tuple(rows.values())
        + tuple(row for row in _resolution_rows() if row.vendor_policy is None),
    )
    promoted = {item.legacy_id: item.validated for item in plan.promotions}
    assert (
        promoted[
            next(key for key in promoted if key.startswith("bk011-"))
        ].applicability.effective_from
        == whatsapp.effective_from
    )
    assert (
        promoted[
            next(key for key in promoted if key.startswith("bk012-"))
        ].applicability.effective_from
        is None
    )


def test_resolution_never_rewrites_embedded_expression() -> None:
    plan = build_resolution_plan(_full_plan(), _resolution_rows())
    for item in plan.promotions:
        assert item.validated.card_version == 2
        assert item.validated.status.value == "validated"
        assert item.validated.retrieval_eligible is True
        assert card_content_digest(item.validated) == card_content_digest(item.candidate)
        assert item.validated.claim == item.candidate.claim
        assert item.validated.distillation_note == item.candidate.distillation_note


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, query: str, params: object = ...) -> None:
        self.calls.append((query, params))


def test_persistence_is_append_only_complete_and_embedding_copy_is_egress_free() -> None:
    plan = build_resolution_plan(_full_plan(), _resolution_rows())
    conn = RecordingConnection()
    persist_resolution_plan(conn, plan)
    queries = [query for query, _ in conn.calls]
    assert sum("INSERT INTO public.knowledge_corpus_versions" in query for query in queries) == 1
    assert sum("INSERT INTO public.knowledge_cards" in query for query in queries) == 36
    assert sum("INSERT INTO public.knowledge_card_sources" in query for query in queries) == 36
    assert sum("INSERT INTO public.knowledge_lifecycle_events" in query for query in queries) == 36
    assert sum("INSERT INTO public.knowledge_corpus_members" in query for query in queries) == 118
    assert not any("UPDATE " in query or "DELETE " in query for query in queries)

    copy_conn = RecordingConnection()
    copy_resolution_embeddings(copy_conn, plan)
    assert len(copy_conn.calls) == 36
    assert all(
        "INSERT INTO public.knowledge_card_embeddings" in query for query, _ in copy_conn.calls
    )
    assert all("SELECT %s" in query for query, _ in copy_conn.calls)
    assert all("UPDATE " not in query and "DELETE " not in query for query, _ in copy_conn.calls)
