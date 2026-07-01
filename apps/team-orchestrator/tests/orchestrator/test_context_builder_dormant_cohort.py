"""VT-490 — dormant-cohort surfacing into the conversational SR lane.

The brain's conversational Sales-Recovery lane (``build_sales_recovery_context``
-> ``run_sales_recovery_agent``, no tools) only ever saw the AGGREGATE
``LedgerSummary`` percentiles — never the per-customer dormant rows — so a
customer-specific win-back request correctly fell through to
``insufficient_data``. VT-490 RE-WIRES the already-approved VT-369 mechanism
(``detect_lapsed_customers`` + ``build_customer_fact_bundle`` +
``CustomerFactBundle``, behind the CL-425 owner-inputs gate / CL-390 redaction)
into the brain's context seam — same data class, same consent envelope, same
single-tenant RLS read. No new privacy primitive.

These tests pin:
  - the constructor populates ``dormant_cohort`` by calling the EXISTING executor
    detection helpers (mocked) behind the CL-425 gate;
  - safe-empty (CL-190) when the gate is closed / read errors / no candidates;
  - the serializer renders ONLY the 5 minimum-necessary fields, with the
    ``_PHONE_SHAPE_RE`` redaction backstop blocking any phone/email shape;
  - the cohort truncates LAST among per-tenant sections — BEFORE the L3/L4 moat;
  - raw cohort rows NEVER land in the composition audit.

Pure-Python: the executor detection helpers + the CL-425 gate + the
``tenant_connection`` read are all mocked, so no DB / LLM is required.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("pydantic")

import orchestrator.context_builder as cb  # noqa: E402 — post importorskip
from orchestrator.agents.sales_recovery_executor import (  # noqa: E402
    _PHONE_SHAPE_RE,
    CustomerFactBundle,
    LapsedCandidate,
)
from orchestrator.context_builder import (  # noqa: E402
    SalesRecoveryContext,
    _build_dormant_cohort,
    build_sales_recovery_context,
    serialize_bundle_for_prompt,
)

_EXEC = "orchestrator.agents.sales_recovery_executor"
_GATE = "orchestrator.memory.l0_writer._owner_inputs_enabled"


class _DummyCM:
    """Stand-in for ``tenant_connection(tenant_id)`` — the mocked detection helpers
    ignore the connection, so this only needs the context-manager protocol."""

    def __enter__(self) -> object:
        return object()

    def __exit__(self, *exc: object) -> bool:
        return False


def _bundle(
    *, name: str | None = "Asha", days: int = 95, spend: int = 80_000,
    customer_id: UUID | None = None,
) -> CustomerFactBundle:
    return CustomerFactBundle(
        customer_id=customer_id or uuid4(),
        display_name=name,
        days_since_last_sale=days,
        last_sale_amount_paise=12_500,
        lifetime_spend_paise=spend,
        business_name="Asha Cafe",
    )


@pytest.fixture
def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the OTHER (DB-backed) section builders + the audit write to safe-empty
    so a ``build_sales_recovery_context`` call needs no DB. Does NOT stub
    ``_build_dormant_cohort`` — the tests that exercise the real helper mock the
    executor functions instead; the tests that exercise truncation override it."""
    monkeypatch.setattr(cb, "_build_recent_campaigns", lambda tid: ([], False))
    monkeypatch.setattr(cb, "_build_pending_owner_inputs", lambda tid: ([], False))
    monkeypatch.setattr(cb, "_build_ledger_summary", lambda tid: (cb.LedgerSummary(), True))
    monkeypatch.setattr(cb, "_build_l3_priors", lambda tid, rid: (cb.L3Priors(), False))
    monkeypatch.setattr(cb, "_build_l4_skills", lambda tid, req: (cb.L4Skills(), False))
    monkeypatch.setattr(cb, "_build_recovery_target_config", lambda tid: (1.1, 50_000))
    monkeypatch.setattr(cb, "_write_composition_audit", lambda **kw: None)


# --- the helper: real _build_dormant_cohort over MOCKED executor helpers -------


def test_build_populates_dormant_cohort_from_detection_helpers(
    monkeypatch: pytest.MonkeyPatch, _no_db: None
) -> None:
    """``build_sales_recovery_context`` populates ``dormant_cohort`` by calling the
    EXISTING VT-369 ``detect_lapsed_customers`` + ``build_customer_fact_bundle``
    (mocked) behind the open CL-425 gate. The frozen bundles flow through to the
    bundle field and the completeness flag flips True."""
    candidates = [
        LapsedCandidate(
            customer_id=uuid4(),
            days_since_last_sale=90 + i,
            last_sale_date=date(2026, 1, 1),
            lifetime_spend_paise=90_000 - i,
        )
        for i in range(3)
    ]
    bundles = {c.customer_id: _bundle(name=f"Cust{i}", spend=90_000 - i) for i, c in enumerate(candidates)}

    monkeypatch.setattr(_GATE, lambda tid: True)
    monkeypatch.setattr(f"{_EXEC}.detect_lapsed_customers", lambda tid, *, conn, limit: candidates)
    monkeypatch.setattr(
        f"{_EXEC}.build_customer_fact_bundle",
        lambda tid, cid, *, conn: bundles[cid],
    )
    monkeypatch.setattr(cb, "tenant_connection", lambda tid: _DummyCM())

    bundle = build_sales_recovery_context(uuid4(), uuid4(), "weekly_cadence", "win back my dormant cafe regulars")

    # one CustomerFactBundle per detected candidate, in detection order — the exact
    # objects the (mocked) executor helpers returned, proving the wiring calls them.
    expected = [bundles[c.customer_id] for c in candidates]
    assert bundle.dormant_cohort == expected
    assert len(bundle.dormant_cohort) == 3
    assert bundle.data_completeness["dormant_cohort"] is True


def test_dormant_cohort_safe_empty_when_owner_inputs_gate_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CL-425 fail-closed: gate FALSE → ``([], False)``; detection is NEVER reached
    (no PII read), so the brain falls back to ``insufficient_data``."""
    called: dict[str, bool] = {"detect": False}

    def _detect(*a: Any, **k: Any) -> list[LapsedCandidate]:
        called["detect"] = True
        return []

    monkeypatch.setattr(_GATE, lambda tid: False)
    monkeypatch.setattr(f"{_EXEC}.detect_lapsed_customers", _detect)

    cohort, ok = _build_dormant_cohort(uuid4())

    assert cohort == []
    assert ok is False
    assert called["detect"] is False  # gate short-circuits before any PII read


def test_dormant_cohort_fail_closed_on_consent_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consent-read error fails CLOSED (safe-empty) — never surface PII on an
    unknown consent state."""

    def _boom(_tid: Any) -> bool:
        raise RuntimeError("consent store unreachable")

    monkeypatch.setattr(_GATE, _boom)

    cohort, ok = _build_dormant_cohort(uuid4())

    assert cohort == []
    assert ok is False


def test_dormant_cohort_safe_empty_when_no_lapsed_customers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate OPEN but zero candidates (the structurally-fail-closed default when
    ``MARKETING_CONSENT_VERSIONS`` is empty) → ``([], False)``."""
    monkeypatch.setattr(_GATE, lambda tid: True)
    monkeypatch.setattr(f"{_EXEC}.detect_lapsed_customers", lambda tid, *, conn, limit: [])
    monkeypatch.setattr(cb, "tenant_connection", lambda tid: _DummyCM())

    cohort, ok = _build_dormant_cohort(uuid4())

    assert cohort == []
    assert ok is False


# --- the serializer: minimum-necessary fields + redaction backstop -------------


def _ctx_with_cohort(cohort: list[CustomerFactBundle]) -> SalesRecoveryContext:
    return SalesRecoveryContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        user_request="recover dormant customers",
        dormant_cohort=cohort,
        data_completeness={"dormant_cohort": bool(cohort)},
    )


def test_serialize_renders_only_five_allowed_fields() -> None:
    """The render carries ONLY customer_id + display_name + days_since_last_sale +
    lifetime_spend_paise + business_name — ``last_sale_amount_paise`` is omitted
    (minimum-necessary)."""
    # Deterministic letters-only customer_id: a random uuid4 rendered into the block can carry
    # an 8+ digit run that the _PHONE_SHAPE_RE backstop (\+?\d{8,}) false-matches (pre-existing
    # flake, ~1/28 runs). Fixed hex-letter id has no digit run → the negative assertions below
    # are stable. Version/variant nibbles (4/8) are isolated, never 8-consecutive.
    m = _bundle(
        name="Priya", days=120, spend=64_000,
        customer_id=UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
    )
    rendered = serialize_bundle_for_prompt(_ctx_with_cohort([m]))

    block = rendered.split("## Dormant cohort", 1)[1].split("\n## ", 1)[0]
    assert "## Dormant cohort" in rendered
    assert f"customer_id={m.customer_id}" in block
    assert "display_name=Priya" in block
    assert "days_since_last_sale=120" in block
    assert "lifetime_spend_paise=64000" in block
    assert "business_name=Asha Cafe" in block
    # minimum-necessary: last_sale_amount_paise must NOT be rendered.
    assert "last_sale_amount_paise" not in block
    assert str(m.last_sale_amount_paise) not in block
    assert "substrate_populated: True" in block
    # CL-390 backstop: no phone/email shape anywhere in the cohort block.
    assert _PHONE_SHAPE_RE.search(block) is None


def test_serialize_phone_shape_backstop_blocks_a_phone_in_display_name() -> None:
    """If a phone-shaped value ever reached a free-text field, the
    ``_PHONE_SHAPE_RE`` backstop raises rather than letting it into the prompt
    (defence-in-depth — CustomerFactBundle carries no phone by construction)."""
    poisoned = _bundle(name="+919321553267")
    with pytest.raises(ValueError, match="redaction backstop"):
        serialize_bundle_for_prompt(_ctx_with_cohort([poisoned]))


def test_serialize_dormant_cohort_empty_renders_count_zero() -> None:
    """Safe-empty cohort still renders the section + a clear ``count: 0`` /
    insufficient-data marker (absence is not a fetch failure)."""
    rendered = serialize_bundle_for_prompt(_ctx_with_cohort([]))
    block = rendered.split("## Dormant cohort", 1)[1].split("\n## ", 1)[0]
    assert "count: 0" in block
    assert "insufficient_data" in block
    assert "substrate_populated: False" in block


# --- truncation: cohort sheds BEFORE the L3/L4 moat ----------------------------


def test_dormant_cohort_truncates_last_before_moat(
    monkeypatch: pytest.MonkeyPatch, _no_db: None
) -> None:
    """An over-budget bundle sheds dormant-cohort rows BEFORE it ever touches the
    L3/L4 moat: after build, the cohort is trimmed but L3 priors + L4 skills
    survive intact (the moat is the most-protected layer; the cohort sheds first
    among the load-bearing sections, after the cheap per-tenant ones)."""
    fat = [_bundle(name="X" * 500, spend=90_000 - i) for i in range(50)]  # 50 fat rows
    monkeypatch.setattr(cb, "_build_dormant_cohort", lambda tid: (list(fat), True))
    monkeypatch.setattr(
        cb,
        "_build_l3_priors",
        lambda tid, rid: (
            cb.L3Priors(available=True, patterns=[{"cohort_key": "cafe|t2|0-30", "metrics": {}, "confidence_band": "low", "n_tenants": 11}], note=""),
            True,
        ),
    )
    monkeypatch.setattr(
        cb,
        "_build_l4_skills",
        lambda tid, req: (
            cb.L4Skills(available=True, skills=[{"id": str(uuid4()), "title": "winback playbook", "tags": [], "priority": 1, "score": 0.9, "excerpt": "..."}], note=""),
            True,
        ),
    )

    bundle = build_sales_recovery_context(uuid4(), uuid4(), "weekly_cadence", "win back dormant")

    assert len(bundle.dormant_cohort) < 50  # cohort was trimmed under the 6400 cap
    assert bundle.l3_priors.available is True  # moat intact
    assert bundle.l4_skills.available is True  # moat intact


# --- CL-390: raw cohort rows never land in the composition audit ---------------


def test_composition_audit_carries_no_raw_cohort_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-customer rows are PROMPT-ONLY. The composition audit may carry the
    section's token COUNT (a counter) but NEVER the raw rows / display names."""
    sentinel = "ZZZSENTINELDISPLAYNAME"
    monkeypatch.setattr(cb, "_build_recent_campaigns", lambda tid: ([], False))
    monkeypatch.setattr(cb, "_build_pending_owner_inputs", lambda tid: ([], False))
    monkeypatch.setattr(cb, "_build_ledger_summary", lambda tid: (cb.LedgerSummary(), True))
    monkeypatch.setattr(cb, "_build_l3_priors", lambda tid, rid: (cb.L3Priors(), False))
    monkeypatch.setattr(cb, "_build_l4_skills", lambda tid, req: (cb.L4Skills(), False))
    monkeypatch.setattr(cb, "_build_recovery_target_config", lambda tid: (1.1, 50_000))
    monkeypatch.setattr(cb, "_build_dormant_cohort", lambda tid: ([_bundle(name=sentinel)], True))

    captured: dict[str, Any] = {}
    monkeypatch.setattr(cb, "_write_composition_audit", lambda **kw: captured.update(kw))

    build_sales_recovery_context(uuid4(), uuid4(), "weekly_cadence", "win back dormant")

    blob = json.dumps(captured, default=str)
    assert sentinel not in blob  # no display name anywhere in the audit payload
    # the section token COUNT is present (a counter, CL-390-safe) and is an int.
    assert isinstance(captured["section_token_counts"]["dormant_cohort"], int)
    assert captured["section_token_counts"]["dormant_cohort"] > 0
