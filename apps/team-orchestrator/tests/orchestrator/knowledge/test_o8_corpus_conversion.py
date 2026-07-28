"""VT-710 deterministic rights-first conversion gates for the audited 118-card corpus."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / "scripts"))

from business_knowledge.convert_o8_candidates import (  # noqa: E402
    CANDIDATE_OUTPUT,
    HISTORICAL_MANIFEST,
    INPUT,
    REPORT_OUTPUT,
    RIGHTS_OUTPUT,
    convert,
)
from orchestrator.knowledge.contracts import (  # noqa: E402
    CardStatus,
    KnowledgeCard,
    SourceClass,
)


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_derived_artifacts_live_in_the_scanned_application_tree() -> None:
    expected_dir = ROOT / "apps" / "team-orchestrator" / "knowledge_corpus"
    assert {CANDIDATE_OUTPUT.parent, RIGHTS_OUTPUT.parent, REPORT_OUTPUT.parent} == {expected_dir}
    assert all(path.is_file() for path in (CANDIDATE_OUTPUT, RIGHTS_OUTPUT, REPORT_OUTPUT))


def test_rights_pass_covers_104_sources_before_all_118_candidates() -> None:
    # These derived artifacts are version-controlled and must remain fully verifiable in CI.
    # The raw archive used to regenerate them is intentionally local-only after the history purge.
    rights = _jsonl(RIGHTS_OUTPUT)
    candidates = _jsonl(CANDIDATE_OUTPUT)
    assert len(rights) == 104
    assert len(candidates) == 118
    assert all(row["rights_pass_completed_before_conversion"] is True for row in rights)
    assert {row["source_id"] for row in rights} == {row["source_id"] for row in candidates}
    assert len({row["content_hash"] for row in rights}) == 104
    assert all(re.fullmatch(r"[0-9a-f]{64}", row["content_hash"]) for row in rights)
    assert all(row["tainted"] is True for row in rights)

    rights_statuses = Counter(row["usage_rights"]["status"] for row in rights)  # type: ignore[index]
    assert rights_statuses == {
        "unknown": 96,
        "live_link_only": 5,
        "permission_granted": 3,
    }


def test_every_committed_candidate_card_validates() -> None:
    candidates = _jsonl(CANDIDATE_OUTPUT)
    cards = [KnowledgeCard.model_validate(row["card"]) for row in candidates]
    assert len({card.card_id for card in cards}) == 118
    assert len({card.card_version_id for card in cards}) == 118
    assert not any(card.retrieval_eligible for card in cards)
    assert not any(card.status is CardStatus.VALIDATED for card in cards)
    assert all(card.provenance.tainted for card in cards)
    assert all(
        re.fullmatch(r"[a-z0-9_]+", dimension)
        for card in cards
        for dimension in (
            card.claim_key.subject,
            card.claim_key.predicate,
            card.claim_key.jurisdiction,
            card.claim_key.population,
            card.claim_key.channel,
        )
    )
    assert all(
        card.status is CardStatus.RESEARCH_ONLY
        for card in cards
        if card.source_class is SourceClass.T4_EXPERIENTIAL
    )


@pytest.mark.skipif(
    not INPUT.is_file() or not HISTORICAL_MANIFEST.is_file(),
    reason="local-only source corpus is unavailable; committed artifacts remain validated",
)
def test_local_source_regeneration_matches_committed_artifacts() -> None:
    """Prove deterministic regeneration when the rights-audited local archive is present."""

    generated_rights, generated_candidates = convert()
    assert _jsonl(RIGHTS_OUTPUT) == generated_rights
    assert _jsonl(CANDIDATE_OUTPUT) == generated_candidates


def test_raw_content_and_legacy_trust_cannot_enter_candidate_card() -> None:
    rows = _jsonl(CANDIDATE_OUTPUT)
    for row in rows:
        card = row["card"]
        assert "raw_text" not in card
        assert "trust_level" not in card
        assert "source_type" not in card
        assert row["pipeline_steps"][0] == "rights_verified"
        assert row["pipeline_steps"][-1] == "candidate_registered"
        assert row["quarantine_ref"].startswith("archive://")


def test_five_live_link_cards_are_rights_blocked() -> None:
    rights = {row["source_id"]: row for row in _jsonl(RIGHTS_OUTPUT)}
    candidates = _jsonl(CANDIDATE_OUTPUT)
    live = [
        row
        for row in candidates
        if rights[row["source_id"]]["usage_rights"]["status"] == "live_link_only"
    ]
    assert {row["legacy_id"] for row in live} == {
        "bk112-gomechanic-independent-financial-truth",
        "bk113-good-glamm-integration-capacity",
        "bk115-starbucks-pause-to-restore-standard",
        "bk119-wework-duration-mismatch-governance",
        "bk121-southwest-recovery-is-product-capacity",
    }
    assert all(row["embedding_state"] == "rights_blocked" for row in live)
