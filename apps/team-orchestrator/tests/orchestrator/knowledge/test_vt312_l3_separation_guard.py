"""VT-312 D3 — source-level guard: the brain's per-tenant ledger call MUST NOT
leak into the fixed L3 cross-tenant cohort plane.

Two planes, two semantics:

  * ``context_builder._build_ledger_summary`` (VT-312 brain-decides) reads the
    tenant's OWN raw recency + spend percentile distributions, per-tenant, under
    RLS — the brain judges dormant / high-value contextually with NO fixed
    threshold.
  * ``knowledge.l3_construction`` builds ANONYMIZED cross-tenant priors on the
    BYPASSRLS service-role pool, bucketing recency into the FIXED ``recency_band``
    k-anon cohort dimension (``l3_types``). A single tenant's numbers must NOT be
    reconstructable from a pattern (Pillar 7).

If ``l3_construction`` ever imported ``context_builder`` or referenced
``_build_ledger_summary`` / the new distribution fields, a per-tenant signal
could contaminate the fixed cross-tenant cohort assignment — collapsing the
separation. This source-level guard fails loud if that coupling is ever
introduced. Mirrors the ``inspect.getsource`` guard pattern of
``tests/orchestrator/test_dsr_purge_substrate.py::test_vt154_unscoped_delete_guard_*``.

Pure source inspection — no DB, no LLM. The separation already holds, so this
test PASSES today; it locks it in.
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("psycopg")  # l3_construction imports psycopg via get_pool


def test_l3_construction_does_not_import_context_builder() -> None:
    """``l3_construction`` must not import the per-tenant Composer module."""
    from orchestrator.knowledge import l3_construction

    src = inspect.getsource(l3_construction)
    assert "context_builder" not in src, (
        "VT-312 D3: l3_construction references `context_builder`. The fixed L3 "
        "cross-tenant cohort plane must NOT depend on the brain's per-tenant "
        "Composer — keep the two planes separate (Pillar 7 / Pillar 8)."
    )


def test_l3_construction_does_not_reference_build_ledger_summary() -> None:
    """The brain's per-tenant ledger call must never be invoked from L3."""
    from orchestrator.knowledge import l3_construction

    src = inspect.getsource(l3_construction)
    assert "_build_ledger_summary" not in src, (
        "VT-312 D3: l3_construction references `_build_ledger_summary`. The "
        "brain's per-tenant raw-distribution read must never feed the fixed "
        "cross-tenant recency_band cohort assignment."
    )


def test_l3_construction_does_not_read_per_tenant_distribution_fields() -> None:
    """The new per-tenant distribution fields must not surface in L3 construction
    — the fixed cohort plane derives recency from the k-anon ``recency_band``,
    never from this tenant's own percentile distribution."""
    from orchestrator.knowledge import l3_construction

    src = inspect.getsource(l3_construction)
    for field in ("recency_days_pctl", "spend_paise_pctl"):
        assert field not in src, (
            f"VT-312 D3: l3_construction references `{field}` — a per-tenant "
            "distribution field. The fixed cross-tenant cohort plane must not "
            "read it; recency bucketing belongs to `recency_band` (l3_types)."
        )
