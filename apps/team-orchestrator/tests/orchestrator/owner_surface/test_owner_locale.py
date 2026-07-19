"""VT-677 — canonical owner-locale module: value space, D1 register mapping, write guards.

Pure unit (DB mocked). The D1 invariant these pin: a hinglish-preference owner NEVER gets
Devanagari on an agent-initiated surface — templates map hinglish→en until Meta approves the
hi-Latn variants; free-form acks serve the hi-Latn register directly.
"""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import pytest

# The DB-mock helpers import the real tenant_connection module (psycopg); freeform_acks pulls the
# send stack. Dep-less smoke skips the module; the full suite runs everything (VT-337 discipline).
pytest.importorskip("psycopg")

from orchestrator.owner_surface import owner_locale as ol  # noqa: E402


# --- deterministic script detection -------------------------------------------------------------


def test_is_devanagari() -> None:
    assert ol.is_devanagari("मेरे customers को message भेजो")
    assert not ol.is_devanagari("mere customers ko message bhejo")  # hi-Latn is NOT Devanagari
    assert not ol.is_devanagari("send my campaign")
    assert not ol.is_devanagari("")


# --- D1 template register -----------------------------------------------------------------------


def test_template_register_d1_mapping() -> None:
    assert ol.template_register("hi") == "hi"  # pure-hi owners keep Devanagari templates
    assert ol.template_register("hinglish") == "en"  # NEVER Devanagari for hinglish-preference
    assert ol.template_register("en") == "en"
    assert ol.template_register("junk") == "en"


# --- resolve: precedence + normalization --------------------------------------------------------


def _patch_conn(monkeypatch: pytest.MonkeyPatch, lang_value: Any) -> None:
    class _Cur:
        def fetchone(self):
            return {"lang": lang_value}

    class _Conn:
        def execute(self, sql, params):
            return _Cur()

    @contextmanager
    def _fake(tenant_id):
        yield _Conn()

    tc_mod = importlib.import_module("orchestrator.db.tenant_connection")
    monkeypatch.setattr(tc_mod, "tenant_connection", _fake)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [("hi", "hi"), ("hinglish", "hinglish"), ("en", "en"), ("fr", "en"), (None, "en")],
)
def test_resolve_normalizes_to_supported(
    monkeypatch: pytest.MonkeyPatch, stored: Any, expected: str
) -> None:
    _patch_conn(monkeypatch, stored)
    assert ol.resolve_owner_locale(uuid4()) == expected


def test_resolve_fails_soft_to_en(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def _boom(tenant_id):
        raise RuntimeError("db down")
        yield

    tc_mod = importlib.import_module("orchestrator.db.tenant_connection")
    monkeypatch.setattr(tc_mod, "tenant_connection", _boom)
    assert ol.resolve_owner_locale(uuid4()) == "en"


# --- write guards: never persist junk -----------------------------------------------------------


def test_writes_reject_unsupported_values() -> None:
    # Rejected BEFORE any DB touch — no mock needed; junk can never reach the columns.
    assert ol.record_observed_language(uuid4(), "klingon") is False
    assert ol.set_explicit_language(uuid4(), "") is False


# --- freeform acks: hinglish register live ------------------------------------------------------


def test_ack_body_serves_hinglish_register() -> None:
    from orchestrator.owner_surface.freeform_acks import ACK_COPY, ack_body

    body = ack_body("support_handoff", "hinglish", ref="RUN123")
    assert body == ACK_COPY["support_handoff"]["hinglish"].format(ref="RUN123")
    assert not ol.is_devanagari(body)  # hi-Latn, by construction
    # And every ack kind carries all three registers.
    for kind, variants in ACK_COPY.items():
        assert {"en", "hi", "hinglish"} <= set(variants), kind


# --- the D1 regression the audit caught: floor language must be EXACT-match ---------------------


def test_floor_language_hinglish_never_devanagari(monkeypatch: pytest.MonkeyPatch) -> None:
    """'hinglish'.startswith('hi') is True — the old floor mapping would have served the
    DEVANAGARI floor to a hinglish-preference owner. Pin the exact-match fix."""
    pytest.importorskip("langchain")
    import orchestrator.agent.onboarding_conductor as oc

    class _Cur:
        def fetchone(self):
            return {"preferred_language": "hinglish", "language_preference": None}

    class _Conn:
        def execute(self, sql, params):
            return _Cur()

    @contextmanager
    def _fake(tenant_id):
        yield _Conn()

    tc_mod = importlib.import_module("orchestrator.db.tenant_connection")
    monkeypatch.setattr(tc_mod, "tenant_connection", _fake)
    assert oc._floor_language(uuid4()) == "en"  # NOT 'hi'


# --- VT-677 phase-2: triage language enum + observed persist ------------------------------------


def test_triage_result_language_field_and_default() -> None:
    pytest.importorskip("anthropic")
    from orchestrator.manager.triage import TriageResult

    # Backward-compat: an older prompt omitting the field parses to 'en'.
    r = TriageResult(outcome="direct_reply")
    assert r.language == "en"
    r2 = TriageResult(outcome="new_task", task_kind="campaign_recovery", language="hinglish")
    assert r2.language == "hinglish"
    with pytest.raises(Exception):
        TriageResult(outcome="direct_reply", language="klingon")


def test_seam_persist_applies_devanagari_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deterministic script override beats the LLM enum: a Devanagari turn persists 'hi'
    even if the classifier said 'en'; a Latin turn persists the classifier's value."""
    pytest.importorskip("anthropic")
    from orchestrator.manager import triage_seam as ts

    recorded: list[str] = []
    monkeypatch.setattr(
        "orchestrator.owner_surface.owner_locale.record_observed_language",
        lambda tid, lang: recorded.append(lang) or True,
    )
    ts._persist_observed_language(uuid4(), "मेरे customers को offer भेजो", "en")
    ts._persist_observed_language(uuid4(), "purane customers ko offer bhejo", "hinglish")
    assert recorded == ["hi", "hinglish"]


def test_campaign_language_never_sourced_from_owner_preference() -> None:
    """Design (e) conformance guard: CUSTOMER campaign copy language is per-COHORT
    (CampaignPlan.message_plan.language, the SR prompt owns it) and must NEVER be sourced from the
    owner's tenants language columns. Structural: the campaign-copy path modules must not import
    the owner-locale resolver. A future import here = conflating owner chat language with customer
    send language — fail loud."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[3] / "src" / "orchestrator"
    campaign_path_files = [
        src / "collapse.py",
        src / "campaign" / "execute.py",
        src / "agent" / "sales_recovery.py",
    ]
    for f in campaign_path_files:
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8")
        assert "resolve_owner_locale" not in text, (
            f"{f.name}: campaign path must not read the OWNER language preference — "
            "campaign language is per-cohort (CampaignPlan.message_plan.language)"
        )
        assert "owner_locale" not in text, (
            f"{f.name}: campaign path must not import owner_surface.owner_locale"
        )


def test_seam_persist_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("anthropic")
    from orchestrator.manager import triage_seam as ts

    def _boom(tid, lang):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "orchestrator.owner_surface.owner_locale.record_observed_language", _boom
    )
    ts._persist_observed_language(uuid4(), "hello", "en")  # must not raise
