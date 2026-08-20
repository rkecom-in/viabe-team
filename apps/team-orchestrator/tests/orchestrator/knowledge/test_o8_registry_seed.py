"""VT-723 — the single writer for knowledge_card_sources, and its stance column."""

from __future__ import annotations

import pytest


def test_vt723_source_edge_records_stance_and_stays_the_only_writer() -> None:
    """VT-723 / migration 202 — a source that REFUTES a claim must be recordable as such.

    This is not bookkeeping. Corroboration promotes a card by counting independent SUPPORTING
    clusters, so an unmarked refutation counts toward promoting the very claim it demolishes — the
    card ends up better-corroborated the more authoritatively it is contradicted. Any counting
    query must filter `supports = true`.

    The second assertion is the one that matters for the next contributor: PR #553 added stance by
    RE-IMPLEMENTING this writer rather than extending it, and the copy inserted `supports` and
    `relevance` columns that did not exist — `UndefinedColumn` on the first row, whole load
    committed nothing. One writer, extended.
    """
    import inspect

    # registry_seed pulls db.tenant_connection -> psycopg, absent in the dep-less smoke suite.
    pytest.importorskip("psycopg")
    from orchestrator.knowledge import registry_seed

    src = inspect.getsource(registry_seed._insert_source_edge)
    # Strip the docstring: it DISCUSSES `relevance` (to say why it is absent), and a naive
    # substring check over the whole source would read that explanation as the defect.
    body = src.split('"""')[-1]
    assert "supports" in body, "stance must be persisted, not implied"
    assert "relevance" not in body, (
        "`relevance` was hardcoded to 1 and read nowhere; an unused column is a future misreading"
    )
    sig = inspect.signature(registry_seed._insert_source_edge)
    assert sig.parameters["supports"].default is True, (
        "default TRUE preserves the meaning of every pre-existing edge, all written by paths that "
        "only ever attach agreeing evidence"
    )
    # The INSERT column list and the bound params must agree — the exact mismatch that made #553's
    # copy raise on its first row.
    assert body.count("%s") == 5
