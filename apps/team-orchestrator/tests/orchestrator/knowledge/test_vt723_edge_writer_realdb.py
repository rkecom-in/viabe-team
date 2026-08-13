"""VT-723 — the ONE edge writer's real SQL, executed against real Postgres.

This file exists because of how the defect it guards escaped. PR #553 added evidence stance by
FORKING ``registry_seed._insert_source_edge``, and the fork's INSERT named ``supports`` and
``relevance`` — columns the schema did not have. Every test in that PR drove a ``RecordingConnection``
that appends the query string and never executes it, so the mismatch stayed invisible until
``--execute`` aborted the whole load on its first row. A writer whose SQL no test ever runs is an
untested writer, however many assertions surround it.

It lives HERE, in the ``orchestrator`` CI job, rather than in ``test_migrations.py``: that job has a
Postgres service AND the full dependency set. The ``migrations`` job installs only psycopg, so
importing ``registry_seed`` there raised ``ModuleNotFoundError: langgraph`` — and a local
``--no-project`` run masked it by resolving the heavy deps from the uv cache (the VT-337 trap the
hook's ``--isolated`` smoke exists to catch, which does not cover this job).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("pydantic")

import psycopg  # noqa: E402

from orchestrator.knowledge.contracts import KnowledgeCard  # noqa: E402
from orchestrator.knowledge.ingestion import CandidateArtifact  # noqa: E402
from orchestrator.knowledge.registry_seed import (  # noqa: E402
    _insert_card,
    _insert_source_edge,
    _json,
)

CORPUS = Path(__file__).resolve().parents[3] / "knowledge_corpus"


def _real_card() -> KnowledgeCard:
    """A real VT-723 card, so the writer is exercised on the shape it actually persists.

    Identity is freshened per run. The registry is append-only — a VT-709 trigger refuses a
    ``knowledge_cards`` hard-delete without a prior rights_removal lifecycle event, which is correct
    and means a test may not tidy up after itself. Reusing the corpus card's deterministic uuid5
    would therefore let edge counts accumulate across runs on a persistent database and make the
    cluster-count assertion pass or fail depending on history.
    """

    line = (
        (CORPUS / "t4_corroboration_candidates.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    card = CandidateArtifact.model_validate(json.loads(line)).card
    fresh = card.model_copy(update={"card_id": str(uuid4()), "card_version_id": str(uuid4())})
    return KnowledgeCard.model_validate(fresh.model_dump(mode="json"))


def _insert_source(conn: Any, source_id: str, source_class: str) -> None:
    conn.execute(
        "INSERT INTO public.knowledge_sources "
        "(id, canonical_url, publisher, source_class, content_hash, acquired_at, usage_rights, "
        " retention_class, tainted, expires_at) "
        "VALUES (%s, %s, 'VT-723 edge-writer test', %s, %s, now(), %s::jsonb, "
        " 'lifecycle_managed', true, "
        " CASE WHEN %s = 't4' THEN now() + interval '90 days' ELSE NULL END) "
        "ON CONFLICT (id) DO NOTHING",
        (
            source_id,
            f"https://example.invalid/vt723/{source_id}",
            source_class,
            uuid4().hex + uuid4().hex,
            _json({"status": "unknown"}),
            source_class,
        ),
    )


@pytest.mark.integration
def test_edge_writer_persists_stance_and_a_refutation_never_counts_as_corroboration(
    _migrated_db,
) -> None:
    """Migration 202's invariant, executed rather than described.

    The second half is the reason the column exists: corroboration promotes a card by counting
    independent SUPPORTING clusters, so an unmarked refutation would count toward promoting the very
    claim it demolishes — the card ends up better-corroborated the more authoritatively it is
    contradicted.
    """
    card = _real_card()
    supporting_source = str(uuid4())
    refuting_source = str(uuid4())

    with psycopg.connect(_migrated_db, autocommit=True) as conn:
        _insert_source(conn, supporting_source, card.source_class.value)
        _insert_source(conn, refuting_source, "t1")
        _insert_card(conn, card, supersedes_card_id=None)

        _insert_source_edge(conn, card, supporting_source)
        _insert_source_edge(conn, card, refuting_source, supports=False)

        rows = dict(
            conn.execute(
                "SELECT source_id::text, supports FROM public.knowledge_card_sources "
                "WHERE card_id = %s",
                (card.card_version_id,),
            ).fetchall()
        )
        assert rows == {supporting_source: True, refuting_source: False}, (
            "the real writer's real SQL must persist both edges with their stance"
        )

        total, supporting = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE supports) "
            "FROM public.knowledge_card_sources WHERE card_id = %s",
            (card.card_version_id,),
        ).fetchone()
        assert (total, supporting) == (2, 1), (
            "corroboration counting must see ONE supporting cluster of two edges — the inversion "
            "migration 202 exists to prevent"
        )

        # Replay semantics, asserted rather than assumed: the edge is keyed (card_id, source_id) ON
        # CONFLICT DO NOTHING, so a re-run can neither duplicate an edge NOR silently overwrite a
        # recorded refutation with the default TRUE. Correcting a stance is a deliberate UPDATE,
        # never a side effect of re-running a load — which also means a load that recorded the wrong
        # stance will not self-heal.
        _insert_source_edge(conn, card, refuting_source, supports=True)
        replayed = conn.execute(
            "SELECT count(*), bool_or(supports) FROM public.knowledge_card_sources "
            "WHERE card_id = %s AND source_id = %s",
            (card.card_version_id, refuting_source),
        ).fetchone()
        assert replayed == (1, False), "replay must neither duplicate the edge nor flip its stance"
