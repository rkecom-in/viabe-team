"""VT-368 Gap-4 — behavioral tests for ``orchestrator.business_plan.generator``.

No live LLM anywhere: the Anthropic call is injected/monkeypatched, and the citation validator
(``orchestrator.business_plan.schema``) is injected as a fake module so these tests pin the
GENERATOR's orchestration contract (gate → generate → validate → strip → retry → degrade →
persist → deliver-best-effort) independently of the validator's internals.

Covered behaviours:
  - import-graph guard: a fresh import of the generator NEVER pulls the onboarding draft store
    (unconfirmed drafts must never become grounding facts) — checked in a subprocess against
    ``sys.modules`` AND against the module's source text;
  - no-facts gate: a tenant with no confirmed business_profile → ``{skipped: no_profile}`` +
    a ``business_plan_skipped`` event, and the LLM is never called;
  - clean path: an injected fake LLM returning a valid plan → persisted v1 through a
    ``write_new_version`` spy with generated_by='gap4_generator', uuid4 item_ids, dense seq,
    status=proposed, provenance origin=llm_v1, and the frozen fact bundle;
  - fabricated-fact path: validate → strip → RETRY ONCE (violations appended to the prompt) →
    still broken → ``schema.degrade_template`` persisted + ``business_plan_generation_degraded``
    logged;
  - idempotency: any existing plan version → ``{skipped: exists}``, LLM never called.

DB substrate mirrors ``tests/orchestrator/onboarding/test_journey.py``: migrations applied once,
DBOS launched so the ``tenant_connection`` pool exists, tenants + L1 grounding seeded via a direct
service-role psycopg connection.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after dependency skip guards

from orchestrator.business_plan import generator, store  # noqa: E402
from orchestrator.observability import log as obs_log  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-368 generator substrate tests skipped",
)

_SRC_DIR = Path(generator.__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so ``graph._pool`` (the substrate the ``tenant_connection``
    path resolves) exists. Mirrors tests/orchestrator/onboarding/test_journey.py."""
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


# --- seeding helpers (direct service-role connection — RLS bypassed at seed) ---


def _new_tenant(dsn: str, *, name: str = "VT-368 generator test") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, whatsapp_number) "
            "VALUES (%s, 'founding', 'trial', now(), 'restaurant', %s) RETURNING id",
            (name, f"+9198{uuid4().int % 10**8:08d}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_grounding(dsn: str, tenant_id: UUID) -> None:
    """Confirmed business_profile + tenant entity + one Zomato listing + has_listing edge —
    the substrate ``_gather_grounding`` reads through the RLS'd L1 query path."""
    from psycopg.types.json import Jsonb

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO l1_entities (tenant_id, entity_type, attributes) VALUES (%s, %s, %s)",
            (
                str(tenant_id),
                "business_profile",
                Jsonb({"business_name": "Test Cafe", "category": "restaurant", "city": "Pune"}),
            ),
        )
        t_row = conn.execute(
            "INSERT INTO l1_entities (tenant_id, entity_type, external_key, attributes) "
            "VALUES (%s, 'tenant', %s, %s) RETURNING id",
            (str(tenant_id), str(tenant_id), Jsonb({"business_name": "Test Cafe"})),
        ).fetchone()
        l_row = conn.execute(
            "INSERT INTO l1_entities (tenant_id, entity_type, external_key, attributes) "
            "VALUES (%s, 'platform_listing', 'zomato-1', %s) RETURNING id",
            (str(tenant_id), Jsonb({"platform": "zomato", "rating": 4.2})),
        ).fetchone()
        assert t_row is not None and l_row is not None
        conn.execute(
            "INSERT INTO l1_relationships (tenant_id, from_entity, to_entity, relationship_type) "
            "VALUES (%s, %s, %s, 'has_listing')",
            (str(tenant_id), str(t_row[0]), str(l_row[0])),
        )


# --- fakes -------------------------------------------------------------------


def _fake_schema(
    *,
    violations_per_call: list[list[str]] | None = None,
    degraded: tuple[dict[str, Any], list[dict[str, Any]]] | None = None,
) -> ModuleType:
    """A fake ``orchestrator.business_plan.schema`` module. ``violations_per_call`` feeds
    successive ``validate_plan`` results (last entry repeats); strip is a no-op pass-through."""
    mod = ModuleType("orchestrator.business_plan.schema")
    seq = violations_per_call or [[]]
    calls = {"validate": 0}

    def validate_plan(summary: Any, roadmap: Any, bundle: Any) -> list[str]:
        idx = min(calls["validate"], len(seq) - 1)
        calls["validate"] += 1
        return list(seq[idx])

    def strip_violations(summary: Any, roadmap: Any, bundle: Any) -> tuple[Any, Any, list[str]]:
        # Mirrors the REAL 3-tuple contract (a 2-tuple fake masked the B1 unpack bug).
        idx = min(calls["validate"], len(seq) - 1)
        calls["validate"] += 1
        return summary, roadmap, list(seq[idx])

    def degrade_template(bundle: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        assert degraded is not None, "degrade_template reached without a configured template"
        return degraded

    mod.validate_plan = validate_plan  # type: ignore[attr-defined]
    mod.strip_violations = strip_violations  # type: ignore[attr-defined]
    mod.degrade_template = degrade_template  # type: ignore[attr-defined]
    return mod


_CLEAN_PLAN_JSON = (
    '{"summary": {"text": "A restaurant in Pune [F2][F3].", '
    '"text_hi": "पुणे में रेस्टोरेंट [F2][F3]।", "cited_facts": ["F2", "F3"], '
    '"headline_metrics": {"zomato_rating": 4.2}}, '
    '"roadmap": ['
    '{"month": 1, "objective": "Reply to Zomato reviews weekly", '
    '"why": "Rated 4.2 on Zomato [F4]", "cited_facts": ["F4"], "owning_agent": "reputation", '
    '"owner_action_needed": false, "owner_action": null, "owner_action_hi": null}, '
    '{"month": 2, "objective": "Win back lapsed regulars", '
    '"why": "Restaurant category [F2]", "cited_facts": ["F2"], '
    '"owning_agent": "sales_recovery", "owner_action_needed": true, '
    '"owner_action": "Share your customer list", '
    '"owner_action_hi": "अपनी ग्राहक सूची साझा करें"}]}'
)

_FABRICATED_PLAN_JSON = (
    '{"summary": {"text": "Rated 4.9 everywhere.", "text_hi": "हर जगह 4.9 रेटिंग।", '
    '"cited_facts": [], "headline_metrics": {"rating": 4.9}}, '
    '"roadmap": [{"month": 1, "objective": "Leverage the 4.9 rating", '
    '"why": "Best rated in the city", "cited_facts": [], "owning_agent": "reputation", '
    '"owner_action_needed": false, "owner_action": null, "owner_action_hi": null}]}'
)


class _SpyLLM:
    """Injectable LLM double — records every (prompt, model) call, replays canned outputs."""

    def __init__(self, outputs: list[str]):
        self.outputs = outputs
        self.calls: list[tuple[str, str]] = []

    def __call__(self, prompt: str, model: str) -> str:
        self.calls.append((prompt, model))
        return self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]


def _forbidden_llm(prompt: str, model: str) -> str:
    raise AssertionError("the LLM must not be called on this path")


@pytest.fixture()
def log_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Spy on log_event (the real writer dispatches async — racy to assert against)."""
    calls: list[dict[str, Any]] = []

    def _spy(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(obs_log, "log_event", _spy)
    return calls


@pytest.fixture()
def write_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Spy replacing store.write_new_version — captures the persisted payload, mints v1."""
    writes: list[dict[str, Any]] = []

    def _spy(tenant_id: Any, **kwargs: Any) -> int:
        writes.append({"tenant_id": tenant_id, **kwargs})
        return 1

    monkeypatch.setattr(store, "write_new_version", _spy)
    return writes


# --- the import-graph guard (no DB needed) -------------------------------------


def test_import_graph_never_pulls_the_draft_store() -> None:
    """A FRESH import of the generator must not import orchestrator.onboarding.draft_profile
    (unconfirmed draft fields are never grounding facts), and the module source must not
    reference it at all. Run in a subprocess so the import graph is genuinely fresh."""
    code = (
        "import sys\n"
        "import orchestrator.business_plan.generator\n"
        "assert 'orchestrator.onboarding.draft_profile' not in sys.modules, "
        "'generator imported the onboarding draft store'\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=False
    )
    assert proc.returncode == 0, f"fresh-import guard failed:\n{proc.stderr}"

    source = Path(generator.__file__).read_text(encoding="utf-8")
    assert "draft_profile" not in source, (
        "generator.py references the onboarding draft store in source"
    )


# --- workflow behaviours (DB substrate) ----------------------------------------


@requires_db
def test_no_facts_gate_skips_and_logs(substrate, log_spy, monkeypatch):  # type: ignore[no-untyped-def]
    """A tenant with NO confirmed business_profile → {skipped: no_profile}, a
    ``business_plan_skipped`` event, and the LLM is never reached."""
    monkeypatch.setattr(generator, "_call_llm", _forbidden_llm)
    tenant = _new_tenant(substrate.dsn, name="no-facts gate")

    result = generator.generate_business_plan_workflow(str(tenant))

    assert result == {"skipped": "no_profile"}
    skipped = [c for c in log_spy if c.get("event_type") == "business_plan_skipped"]
    assert len(skipped) == 1, f"expected one business_plan_skipped; got {log_spy}"
    assert str(skipped[0].get("tenant_id")) == str(tenant)
    assert skipped[0].get("component") == "business_plan"
    assert (skipped[0].get("payload") or {}).get("reason") == "no_profile"


@requires_db
def test_clean_plan_persists_v1(substrate, log_spy, write_spy, monkeypatch):  # type: ignore[no-untyped-def]
    """Injected clean LLM output → ONE call, validated, persisted as v1 with
    generated_by='gap4_generator', the frozen fact bundle, uuid4 item_ids, dense seq,
    status=proposed, provenance origin=llm_v1; ``business_plan_generated`` logged."""
    monkeypatch.setitem(
        sys.modules, "orchestrator.business_plan.schema", _fake_schema(violations_per_call=[[]])
    )
    llm = _SpyLLM([_CLEAN_PLAN_JSON])
    monkeypatch.setattr(generator, "_call_llm", llm)

    tenant = _new_tenant(substrate.dsn, name="clean plan v1")
    _seed_grounding(substrate.dsn, tenant)

    result = generator.generate_business_plan_workflow(str(tenant))

    assert result == {"version": 1}
    assert len(llm.calls) == 1, "a clean plan must not trigger the retry"
    assert llm.calls[0][1] == "claude-haiku-4-5", "non-production env must resolve the test slot"
    assert "<facts>" in llm.calls[0][0] and "[F1]" in llm.calls[0][0]

    assert len(write_spy) == 1
    write = write_spy[0]
    assert write["generated_by"] == "gap4_generator"
    assert write["model_id"] == "claude-haiku-4-5"

    bundle = write["fact_bundle"]
    by_key = {f["key"]: f for f in bundle.values()}
    assert by_key["category"]["value"] == "restaurant"
    assert by_key["zomato_rating"]["value"] == 4.2
    assert by_key["zomato_rating"]["source"] == "platform_listing"
    assert by_key["listing_count"]["value"] == 1, "derived counts are computed in Python"

    roadmap = write["roadmap"]
    assert [i["seq"] for i in roadmap] == [1, 2], "seq must be dense 1..N"
    assert len({i["item_id"] for i in roadmap}) == 2
    for item in roadmap:
        assert UUID(item["item_id"]).version == 4
        assert 1 <= item["month"] <= 6
        assert len(item["objective"]) <= 120
        assert item["owning_agent"] in store.OWNING_AGENTS
        assert item["status"] == "proposed"
        assert item["provenance"]["origin"] == "llm_v1"
        assert item["provenance"]["prev_version"] is None
    no_action = roadmap[0]
    assert no_action["owner_action_needed"] is False
    assert no_action["owner_action"] is None and no_action["owner_action_hi"] is None
    with_action = roadmap[1]
    assert with_action["owner_action_needed"] is True
    assert with_action["owner_action"] == "Share your customer list"

    generated = [c for c in log_spy if c.get("event_type") == "business_plan_generated"]
    assert len(generated) == 1
    assert (generated[0].get("payload") or {}).get("version") == 1


@requires_db
def test_fabricated_fact_strips_retries_then_degrades(substrate, log_spy, write_spy, monkeypatch):  # type: ignore[no-untyped-def]
    """An LLM that fabricates a rating on BOTH attempts: validate flags it, strip can't fix it,
    the retry prompt carries the violations, and the persisted plan is schema.degrade_template's
    output + a ``business_plan_generation_degraded`` event."""
    degraded_summary = {
        "text": "degraded",
        "text_hi": "degraded-hi",
        "cited_facts": [],
        "headline_metrics": {},
    }
    fake = _fake_schema(
        violations_per_call=[["fabricated_fact: rating 4.9 is not in the fact bundle"]],
        degraded=(degraded_summary, []),
    )
    monkeypatch.setitem(sys.modules, "orchestrator.business_plan.schema", fake)
    llm = _SpyLLM([_FABRICATED_PLAN_JSON])
    monkeypatch.setattr(generator, "_call_llm", llm)

    tenant = _new_tenant(substrate.dsn, name="fabricated rating degrades")
    _seed_grounding(substrate.dsn, tenant)

    result = generator.generate_business_plan_workflow(str(tenant))

    assert result == {"version": 1}
    assert len(llm.calls) == 2, "exactly ONE retry after a failed validate+strip"
    assert "fabricated_fact" in llm.calls[1][0], (
        "the retry prompt must carry the validator's violations"
    )

    assert len(write_spy) == 1
    assert write_spy[0]["summary"] == degraded_summary, "the degraded template must be persisted"
    assert write_spy[0]["roadmap"] == []

    degraded_events = [
        c for c in log_spy if c.get("event_type") == "business_plan_generation_degraded"
    ]
    assert len(degraded_events) == 1, f"expected one degraded event; got {log_spy}"
    payload = degraded_events[0].get("payload") or {}
    assert any("fabricated_fact" in v for v in payload.get("violations", []))


@requires_db
def test_idempotent_existing_plan_skips(substrate, log_spy, monkeypatch):  # type: ignore[no-untyped-def]
    """A tenant with ANY existing plan version → {skipped: exists}; no LLM call, no new write.
    Seeds v1 through the REAL store.write_new_version (also exercising migration 124)."""
    monkeypatch.setattr(generator, "_call_llm", _forbidden_llm)
    tenant = _new_tenant(substrate.dsn, name="idempotent skip")
    _seed_grounding(substrate.dsn, tenant)

    v1 = store.write_new_version(
        tenant,
        summary={"text": "seed", "text_hi": "seed", "cited_facts": [], "headline_metrics": {}},
        roadmap=[],
        fact_bundle={},
        generated_by="test_seed",
    )
    assert v1 == 1

    result = generator.generate_business_plan_workflow(str(tenant))

    assert result == {"skipped": "exists"}
    assert store.get_active_plan(tenant).version == 1  # type: ignore[union-attr]
    assert not [c for c in log_spy if c.get("event_type") == "business_plan_generated"]


@requires_db
def test_clean_plan_through_REAL_schema_not_degraded(substrate, log_spy, write_spy, monkeypatch):  # type: ignore[no-untyped-def]
    """INTEGRATION (no schema fake — the fakes masked B1+B2): the compliant fixture, with its inline
    [Fid] citation markers and bundle-grounded numbers, must pass the REAL validator and persist
    NON-degraded. Catches (B1) the strip_violations 3-tuple contract and (B2) citation markers being
    flagged as ungrounded tokens — either regression degrades every clean generation to the template."""
    llm = _SpyLLM([_CLEAN_PLAN_JSON])
    monkeypatch.setattr(generator, "_call_llm", llm)

    tenant = _new_tenant(substrate.dsn, name="real schema integration")
    _seed_grounding(substrate.dsn, tenant)

    result = generator.generate_business_plan_workflow(str(tenant))

    assert result == {"version": 1}
    assert len(llm.calls) == 1, "the clean fixture must pass the REAL validator on the first call"
    assert not [c for c in log_spy if c.get("event_type") == "business_plan_generation_degraded"], (
        "a compliant plan must NEVER degrade through the real schema"
    )
    write = write_spy[0]
    assert write["roadmap"], "the real path must persist the LLM roadmap, not the empty template"
    assert write["summary"]["text"].strip(), "summary text survives the real validator"


def test_bundle_builder_has_no_customer_data_path():
    """VT-370 risk-#1 SHIP-BLOCKING acceptance (the grounded-text laundering audit): the Gap-4
    fact bundle is the grounding validator's whitelist — if customer PII entered the bundle, the
    validator would pass it into plan TEXT, which the VTR reads through the vtr_business_plan
    view (bypassing the column-level PII-gating). Structural guarantee: the generator has NO
    customer-data code path — it reads confirmed business_profile scalars + tenant business_name
    + listing platform/rating only. Residual (flagged, accepted): owner-TYPED text in their own
    profile answers is owner-volunteered, not a system leak."""
    import pathlib

    src = pathlib.Path(generator.__file__).read_text(encoding="utf-8")
    for banned in (
        "CustomersWrapper", "customer_ledger", "agent_customer_contacts",
        "FROM customers", "phone_e164", "display_name",
    ):
        assert banned not in src, f"generator.py must not read customer data ({banned})"
