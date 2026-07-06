"""VT-609 (Loop Package 4) — DB-backed behavioral tests for the onboarding_conductor SPECIALIST's
write-tool helpers added to ``orchestrator.onboarding.journey``: ``record_extracted_answer``,
``record_field_skip``, ``confirm_field_answer``. These are the deterministic functions the
specialist's ``extract_owner_answer`` / ``record_skip`` / ``record_answer`` / ``apply_correction``
tools delegate to — the SAME state (``onboarding_journey``) and the SAME promotion gate
(``confirm_draft``) the pre-VT-609 interceptor uses, just without touching cursor/question_queue.

Mirrors the substrate pattern in ``test_journey.py`` / ``test_journey_paced_flow.py``: migrations
applied once, DBOS launched so the ``tenant_connection`` pool exists, tenants seeded via a direct
service-role (BYPASSRLS) psycopg connection.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-609 journey specialist-tool tests skipped",
)


@pytest.fixture(autouse=True)
def _default_not_yet_complete(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """VT-609's ``_maybe_complete_from_specialist`` re-checks ``conductor.profile_collection_complete``
    on every write — which, with NO real Anthropic key in this test env, degrades to "no gap
    candidates" (question_brain's own gap-source fails soft) and would otherwise complete the
    profile after the FIRST write in every test here, silently transitioning status to 'complete'
    and breaking every multi-write test's "still active" assumption. Default every test to "not yet
    complete" (a per-test monkeypatch inside a completion-specific test still overrides this)."""
    import orchestrator.onboarding.conductor as conductor_mod

    monkeypatch.setattr(conductor_mod, "profile_collection_complete", lambda **kwargs: False)
    yield


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str, *, name: str, business_type: str = "restaurant") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, whatsapp_number) "
            "VALUES (%s, 'founding', 'trial', now(), %s, %s) RETURNING id",
            (name, business_type, f"+9199{uuid4().int % 10**8:08d}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _start_active_journey(dsn: str, tenant_id: UUID) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO onboarding_journey (tenant_id, status, question_queue, cursor, "
            "answers, skipped) VALUES (%s, 'active', '[]'::jsonb, 0, '{}'::jsonb, '[]'::jsonb)",
            (str(tenant_id),),
        )


def _journey_row(dsn: str, tenant_id: UUID) -> dict[str, Any] | None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, cursor, answers, skipped FROM onboarding_journey WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    return {"status": row[0], "cursor": row[1], "answers": dict(row[2] or {}), "skipped": list(row[3] or [])}


def _canonical_profile_attributes(dsn: str, tenant_id: UUID) -> dict[str, Any] | None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT attributes FROM l1_entities WHERE tenant_id = %s AND entity_type = 'business_profile'",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    return dict(row[0] or {})


# --- record_extracted_answer -------------------------------------------------------------------


def test_record_extracted_answer_records_without_promoting(substrate):  # type: ignore[no-untyped-def]
    from orchestrator.onboarding.journey import record_extracted_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 extract")
    _start_active_journey(substrate.dsn, tenant)

    out = record_extracted_answer(tenant, "hours", "9am-9pm")
    assert out["recorded"] is True
    assert out["field"] == "hours"

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["answers"]["hours"] == "9am-9pm"
    assert row["cursor"] == 0  # cursor/question_queue untouched (VT-609 — no queue to walk)
    # Not promoted to canonical — extract_owner_answer is the UNCONFIRMED gap-fill path.
    assert _canonical_profile_attributes(substrate.dsn, tenant) is None


def test_record_extracted_answer_rejects_bare_negative_value(substrate):  # type: ignore[no-untyped-def]
    """Defense-in-depth: a bare 'no'/'nope' passed AS the value is never recorded as fact (mirrors
    the walker's own is_bare_no_confirm / _is_bare_greeting guards)."""
    from orchestrator.onboarding.journey import record_extracted_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 extract bare-no")
    _start_active_journey(substrate.dsn, tenant)

    out = record_extracted_answer(tenant, "hours", "no")
    assert out == {"recorded": False}
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert "hours" not in row["answers"]


def test_record_extracted_answer_rejects_bare_greeting_value(substrate):  # type: ignore[no-untyped-def]
    """The live 'Hi -> category' bug's class: a bare greeting passed AS the value (a mis-sequencing
    tool call) is never recorded as fact either — mirrors the walker's own _is_bare_greeting guard."""
    from orchestrator.onboarding.journey import record_extracted_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 extract bare-greeting")
    _start_active_journey(substrate.dsn, tenant)

    out = record_extracted_answer(tenant, "hours", "namaste")
    assert out == {"recorded": False}
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert "hours" not in row["answers"]


def test_record_extracted_answer_records_greeting_mixed_with_substance(substrate):  # type: ignore[no-untyped-def]
    """A greeting MIXED with substantive content is NOT a bare greeting — only a value that is
    ENTIRELY greeting/rejection tokens is rejected (mirrors the walker's own mixed-content carve-out)."""
    from orchestrator.onboarding.journey import record_extracted_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 extract greeting mixed")
    _start_active_journey(substrate.dsn, tenant)

    out = record_extracted_answer(tenant, "hours", "hi 9am to 11pm")
    assert out["recorded"] is True
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["answers"]["hours"] == "hi 9am to 11pm"


def test_record_extracted_answer_no_op_on_inactive_journey(substrate):  # type: ignore[no-untyped-def]
    from orchestrator.onboarding.journey import record_extracted_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 extract inactive")
    # No journey row at all.
    out = record_extracted_answer(tenant, "hours", "9am-9pm")
    assert out == {"recorded": False}


def test_record_extracted_answer_rejects_dunder_prefixed_field(substrate):  # type: ignore[no-untyped-def]
    """VT-609 fix round (MINOR) — a ``__``-prefixed field name is RESERVED bookkeeping (the
    populate-first / paced-flow sentinels). Writing one directly would corrupt journey state and
    crash a later ``populate_profile_from_draft`` call (its merge assumes ``__populated__``'s
    stored value is a per-field dict)."""
    from orchestrator.onboarding.journey import record_extracted_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 extract dunder")
    _start_active_journey(substrate.dsn, tenant)

    out = record_extracted_answer(tenant, "__populated__", "not-a-dict")
    assert out == {"recorded": False}
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert "__populated__" not in row["answers"]


def test_record_extracted_answer_still_accepts_a_volunteered_out_of_order_field(substrate):  # type: ignore[no-untyped-def]
    """The dunder-prefix guard must NOT over-reach into rejecting a legitimate, ordinary business-
    context field the registry hasn't presented as a question yet — the product design explicitly
    requires accepting a volunteered/out-of-order answer (and gap fields are LLM-reasoned per
    business type; there is no static enum to check a field name against without making this
    write's availability depend on a live LLM call)."""
    from orchestrator.onboarding.journey import record_extracted_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 extract volunteered")
    _start_active_journey(substrate.dsn, tenant)

    out = record_extracted_answer(tenant, "a_field_never_asked_about", "some value")
    assert out["recorded"] is True
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["answers"]["a_field_never_asked_about"] == "some value"


# --- record_field_skip --------------------------------------------------------------------------


def test_record_field_skip_defers_field(substrate):  # type: ignore[no-untyped-def]
    from orchestrator.onboarding.journey import record_field_skip

    tenant = _new_tenant(substrate.dsn, name="VT-609 skip")
    _start_active_journey(substrate.dsn, tenant)

    out = record_field_skip(tenant, "website")
    assert out["recorded"] is True
    assert out["field"] == "website"
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["skipped"] == ["website"]

    # Idempotent — skipping the same field twice does not duplicate the entry.
    out2 = record_field_skip(tenant, "website")
    assert out2["recorded"] is True
    assert out2["field"] == "website"
    row2 = _journey_row(substrate.dsn, tenant)
    assert row2 is not None
    assert row2["skipped"] == ["website"]


def test_record_field_skip_rejects_dunder_prefixed_field(substrate):  # type: ignore[no-untyped-def]
    """VT-609 fix round (MINOR) — same reserved-namespace guard as record_extracted_answer."""
    from orchestrator.onboarding.journey import record_field_skip

    tenant = _new_tenant(substrate.dsn, name="VT-609 skip dunder")
    _start_active_journey(substrate.dsn, tenant)

    out = record_field_skip(tenant, "__flow__")
    assert out == {"recorded": False}
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert "__flow__" not in row["skipped"]


# --- confirm_field_answer -----------------------------------------------------------------------


def test_confirm_field_answer_promotes_valid_business_type(substrate):  # type: ignore[no-untyped-def]
    from orchestrator.onboarding.journey import confirm_field_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 confirm valid")
    _start_active_journey(substrate.dsn, tenant)

    out = confirm_field_answer(tenant, "city", "Pune")
    assert out["recorded"] is True
    assert out["promoted"] is True
    assert out["field"] == "city"

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["answers"]["city"] == "Pune"
    attrs = _canonical_profile_attributes(substrate.dsn, tenant)
    assert attrs is not None
    assert attrs["city"] == "Pune"


def test_confirm_field_answer_never_asserts_off_taxonomy_business_type(substrate):  # type: ignore[no-untyped-def]
    """CL-390 never-assert: an off-taxonomy business_type is recorded as a plain answer (the
    conversation substrate) but NEVER promoted to the canonical profile as fact."""
    from orchestrator.onboarding.journey import confirm_field_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 confirm off-taxonomy")
    _start_active_journey(substrate.dsn, tenant)

    out = confirm_field_answer(tenant, "business_type", "not-a-real-taxonomy-value-xyz")
    assert out["recorded"] is True
    assert out["promoted"] is False

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["answers"]["business_type"] == "not-a-real-taxonomy-value-xyz"
    # NEVER asserted as canonical fact.
    attrs = _canonical_profile_attributes(substrate.dsn, tenant)
    assert attrs is None or "business_type" not in attrs


def test_confirm_field_answer_rejects_bare_negative_value(substrate):  # type: ignore[no-untyped-def]
    from orchestrator.onboarding.journey import confirm_field_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 confirm bare-no")
    _start_active_journey(substrate.dsn, tenant)

    out = confirm_field_answer(tenant, "city", "nope")
    assert out == {"recorded": False, "promoted": False}
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert "city" not in row["answers"]


def test_confirm_field_answer_rejects_bare_greeting_value(substrate):  # type: ignore[no-untyped-def]
    """VT-569a's class at the promotion gate: a bare greeting is never confirmed/promoted as a
    field value either."""
    from orchestrator.onboarding.journey import confirm_field_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 confirm bare-greeting")
    _start_active_journey(substrate.dsn, tenant)

    out = confirm_field_answer(tenant, "city", "hello")
    assert out == {"recorded": False, "promoted": False}
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert "city" not in row["answers"]
    assert _canonical_profile_attributes(substrate.dsn, tenant) is None


def test_confirm_field_answer_rejects_bare_affirmation_value(substrate):  # type: ignore[no-untyped-def]
    """VT-477's class: a bare "yes"/"correct" must never itself be recorded as the field's value —
    the walker substitutes ``draft_value`` for a confirm-"yes"; a tool call has no draft_value slot
    to substitute from, so the caller (the specialist) MUST pass the actual value — a bare
    affirmation is refused outright rather than asserted as fact."""
    from orchestrator.onboarding.journey import confirm_field_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 confirm bare-yes")
    _start_active_journey(substrate.dsn, tenant)

    out = confirm_field_answer(tenant, "city", "yes")
    assert out == {"recorded": False, "promoted": False}
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert "city" not in row["answers"]
    assert _canonical_profile_attributes(substrate.dsn, tenant) is None


def test_confirm_field_answer_correction_overwrites_prior_value(substrate):  # type: ignore[no-untyped-def]
    """apply_correction (the agent tool) calls this exact function — an owner correction always
    overwrites a prior (confirmed or populated) value, mirroring populate-first's "edits-forever"
    invariant."""
    from orchestrator.onboarding.journey import confirm_field_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 correction")
    _start_active_journey(substrate.dsn, tenant)

    confirm_field_answer(tenant, "city", "Mumbai")
    out = confirm_field_answer(tenant, "city", "Pune")
    assert out["recorded"] is True
    assert out["promoted"] is True
    assert out["field"] == "city"

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["answers"]["city"] == "Pune"
    attrs = _canonical_profile_attributes(substrate.dsn, tenant)
    assert attrs is not None
    assert attrs["city"] == "Pune"


def test_confirm_field_answer_rejects_dunder_prefixed_field(substrate):  # type: ignore[no-untyped-def]
    """VT-609 fix round (CRITICAL/MINOR audit finding) — the exact bug cited: a caller passing the
    reserved ``__populated__`` sentinel AS a field name must never reach the promotion gate (it
    would both corrupt journey bookkeeping and be promoted to canonical, asserting a bookkeeping
    blob as a real business-profile field)."""
    from orchestrator.onboarding.journey import confirm_field_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 confirm dunder")
    _start_active_journey(substrate.dsn, tenant)

    out = confirm_field_answer(tenant, "__populated__", "not-a-dict")
    assert out == {"recorded": False, "promoted": False}
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert "__populated__" not in row["answers"]
    assert _canonical_profile_attributes(substrate.dsn, tenant) is None


# --- VT-609 ruling: the deterministic completion transition -------------------------------------
# The specialist has no "finish" tool to forget — completion is a SIDE EFFECT of every successful
# write, driven by the SAME pure conductor.profile_collection_complete check profile_completion_check
# reads. Mirrors test_journey.py::test_handle_reply_completion_fires_gap4_seam's own invariant
# (queue exhaustion -> status='complete' + the Gap-4 seam) for the specialist's write path.


def test_confirm_field_answer_completes_profile_and_fires_gap4_seam(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    import orchestrator.onboarding.conductor as conductor_mod
    from orchestrator.observability import log as obs_log
    from orchestrator.onboarding.journey import confirm_field_answer, get_journey

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(obs_log, "log_event", lambda **kwargs: calls.append(kwargs))
    # Deterministically force "this was the last field" — the pure check itself is conductor.py's
    # own job (unit-tested in test_conductor.py); this test proves the SPECIALIST WRITE PATH acts
    # on it, not that the check's own logic is correct.
    monkeypatch.setattr(conductor_mod, "profile_collection_complete", lambda **kwargs: True)

    tenant = _new_tenant(substrate.dsn, name="VT-609 specialist completion")
    _start_active_journey(substrate.dsn, tenant)

    out = confirm_field_answer(tenant, "city", "Pune")
    assert out["profile_completed"] is True

    g = get_journey(tenant)
    assert g is not None
    assert g["status"] == "complete", "the specialist write path must transition status on completion"

    completed = [c for c in calls if c.get("event_type") == "onboarding_journey_completed"]
    assert len(completed) == 1
    assert str(completed[0].get("tenant_id")) == str(tenant)


def test_confirm_field_answer_does_not_complete_when_check_says_no(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    import orchestrator.onboarding.conductor as conductor_mod
    from orchestrator.onboarding.journey import confirm_field_answer, get_journey

    monkeypatch.setattr(conductor_mod, "profile_collection_complete", lambda **kwargs: False)

    tenant = _new_tenant(substrate.dsn, name="VT-609 specialist not-yet-complete")
    _start_active_journey(substrate.dsn, tenant)

    out = confirm_field_answer(tenant, "city", "Pune")
    assert out["profile_completed"] is False

    g = get_journey(tenant)
    assert g is not None
    assert g["status"] == "active"


def test_record_field_skip_can_itself_complete_the_profile(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """A skip can be the LAST thing needed — a skipped field counts as resolved."""
    import orchestrator.onboarding.conductor as conductor_mod
    from orchestrator.onboarding.journey import get_journey, record_field_skip

    monkeypatch.setattr(conductor_mod, "profile_collection_complete", lambda **kwargs: True)

    tenant = _new_tenant(substrate.dsn, name="VT-609 skip completes")
    _start_active_journey(substrate.dsn, tenant)

    out = record_field_skip(tenant, "website")
    assert out["profile_completed"] is True

    g = get_journey(tenant)
    assert g is not None
    assert g["status"] == "complete"
