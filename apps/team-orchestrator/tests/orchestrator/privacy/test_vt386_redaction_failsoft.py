"""VT-386 — redaction-registry health + fail-soft-split outage handling.

Covers the four parts as PURE-PYTHON / dep-less unit tests (these modules
import nothing heavier than stdlib + pydantic-free privacy code):

  A — PII-free health counters (``privacy/redaction_health.py``).
  B — the fail-soft split in the redactor (``registry_down`` → known name-keys
      stripped to ``<name:registry_down>``) + the §B trigger shape.
  C — registry-aware Detector-5 bigram name match helper.
  D — runner inbound-redaction helper threads the walker + registry_down.

The synthetic-outage CANARY (B + the cross-cutting PII boundary) is
``test_outage_canary_*`` below: it monkeypatches the registry build to FAIL and
asserts (1) known name-key fields stripped to the sentinel, (2) NO real name
value reaches any redacted output, (3) the §A counter bumped build_error +
pattern_only, all against a synthetic record carrying a real-LOOKING name +
phone. Synthetic PII only (CL-422).

The alert-dispatch + DB-backed sweep canaries are DB-gated and live in the
real-PG suite / the parent's live canary run.
"""

from __future__ import annotations

import pytest

# Dep-less: the redactor + health modules are stdlib-only; the runner helper
# import path is guarded by importorskip where it pulls pydantic/dbos.
from orchestrator.privacy import redaction_health as rh
from orchestrator.privacy.pii_redactor import (
    _REGISTRY_DOWN_SENTINEL,
    redact,
)

# --- synthetic PII only (CL-422) -------------------------------------------
_SYNTH_NAME = "Test Customer"
_SYNTH_OWNER = "Synthetic Owner"
_SYNTH_PHONE = "+910000000000"
_SYNTH_TENANT = "00000000-0000-0000-0000-0000000000aa"


def _synth_registry(*names: str):
    """A synthetic name-registry predicate matching the real make_name_registry
    contract: case-folded exact match (the predicate casefolds its input)."""
    folded = {n.casefold() for n in names}
    return lambda text: text.casefold() in folded


def setup_function() -> None:
    rh.reset()


# ===========================================================================
# Part A — PII-free health counters
# ===========================================================================

def test_a_registry_outcome_counter_counts_labels_only():
    rh.record_registry_outcome(_SYNTH_TENANT, rh.RegistryOutcome.OK)
    rh.record_registry_outcome(_SYNTH_TENANT, rh.RegistryOutcome.OK)
    rh.record_registry_outcome(_SYNTH_TENANT, rh.RegistryOutcome.BUILD_ERROR)
    assert rh.registry_outcome_count(_SYNTH_TENANT, rh.RegistryOutcome.OK) == 2
    assert rh.registry_outcome_count(_SYNTH_TENANT, rh.RegistryOutcome.BUILD_ERROR) == 1
    assert (
        rh.registry_outcome_count(_SYNTH_TENANT, rh.RegistryOutcome.UNDEFINED_TABLE)
        == 0
    )


def test_a_redaction_mode_counter_and_leak_ratio():
    rh.record_redaction_mode(_SYNTH_TENANT, rh.RedactionMode.FULL)
    rh.record_redaction_mode(_SYNTH_TENANT, rh.RedactionMode.FULL)
    rh.record_redaction_mode(_SYNTH_TENANT, rh.RedactionMode.PATTERN_ONLY)
    # 1 pattern_only of 3 writes.
    assert rh.leak_exposure_ratio(_SYNTH_TENANT) == pytest.approx(1 / 3)


def test_a_leak_ratio_zero_when_no_writes():
    assert rh.leak_exposure_ratio("tenant-with-no-writes") == 0.0


def test_a_degraded_write_count_sums_build_error_and_undefined_table():
    rh.record_registry_outcome(_SYNTH_TENANT, rh.RegistryOutcome.BUILD_ERROR)
    rh.record_registry_outcome(_SYNTH_TENANT, rh.RegistryOutcome.UNDEFINED_TABLE)
    rh.record_registry_outcome(_SYNTH_TENANT, rh.RegistryOutcome.OK)  # not degraded
    assert rh.degraded_write_count(_SYNTH_TENANT) == 2


def test_a_snapshot_is_pii_free_counts_and_labels_only():
    rh.record_registry_outcome(_SYNTH_TENANT, rh.RegistryOutcome.BUILD_ERROR)
    rh.record_redaction_mode(_SYNTH_TENANT, rh.RedactionMode.PATTERN_ONLY)
    snap = rh.snapshot()
    # The whole snapshot, stringified, must contain NO synthetic PII value.
    blob = repr(snap)
    assert _SYNTH_NAME not in blob
    assert _SYNTH_PHONE not in blob
    # Shape: tenant_id -> {label: int}.
    assert snap["registry_outcomes"][_SYNTH_TENANT]["build_error"] == 1
    assert snap["redaction_modes"][_SYNTH_TENANT]["pattern_only"] == 1
    for dim in snap.values():
        for labels in dim.values():
            for label, count in labels.items():
                assert isinstance(label, str)
                assert isinstance(count, int)


# ===========================================================================
# Part B — the fail-soft split in the redactor
# ===========================================================================

def test_b_registry_down_strips_known_name_keys_to_sentinel():
    rec = {"customer_name": _SYNTH_NAME, "owner_name": _SYNTH_OWNER}
    out = redact(rec, name_registry=None, registry_down=True)
    assert out["customer_name"] == _REGISTRY_DOWN_SENTINEL
    assert out["owner_name"] == _REGISTRY_DOWN_SENTINEL
    # The real name never survives.
    assert _SYNTH_NAME not in repr(out)
    assert _SYNTH_OWNER not in repr(out)


def test_b_registry_down_false_keeps_vt101_behaviour_for_no_registry():
    # Regression guard: the MANY no-tenant-context callers (name_registry=None,
    # registry_down=False default) must keep VT-101 byte-identical output — a
    # length-hint token, NOT the registry_down sentinel.
    rec = {"customer_name": _SYNTH_NAME}
    out = redact(rec)  # both defaults
    assert out["customer_name"] == f"<redacted:customer_name:len={len(_SYNTH_NAME)}>"
    assert out["customer_name"] != _REGISTRY_DOWN_SENTINEL
    assert _SYNTH_NAME not in repr(out)


def test_b_registry_down_phone_still_hashed_and_pattern_redaction_runs():
    rec = {"customer_name": _SYNTH_NAME, "phone": _SYNTH_PHONE, "note": _SYNTH_PHONE}
    out = redact(rec, name_registry=None, registry_down=True)
    assert out["customer_name"] == _REGISTRY_DOWN_SENTINEL
    assert out["phone"].startswith("phone_tok_")  # known-key phone hashed
    assert _SYNTH_PHONE not in out["note"]  # pattern phone redacted in free text
    assert _SYNTH_PHONE not in repr(out)


def test_b_sentinel_is_idempotent():
    once = redact({"customer_name": _SYNTH_NAME}, registry_down=True)
    twice = redact(once, registry_down=True)
    assert twice["customer_name"] == _REGISTRY_DOWN_SENTINEL


def test_b_registry_down_works_nested():
    rec = {"event": {"payload": [{"customer_name": _SYNTH_NAME}]}}
    out = redact(rec, registry_down=True)
    assert out["event"]["payload"][0]["customer_name"] == _REGISTRY_DOWN_SENTINEL
    assert _SYNTH_NAME not in repr(out)


def test_b_trigger_shape_is_pii_free():
    pytest.importorskip("psycopg")  # triggers imports get_pool chain lazily
    from uuid import UUID

    from orchestrator.alerts.triggers import (
        build_registry_unavailable_trigger,
        severity_for,
    )

    rh.record_registry_outcome(_SYNTH_TENANT, rh.RegistryOutcome.BUILD_ERROR)
    trig = build_registry_unavailable_trigger(UUID(_SYNTH_TENANT))
    assert trig.trigger_kind == "redaction_registry_unavailable"
    assert trig.severity == "critical"
    assert severity_for("redaction_registry_unavailable") == "critical"
    # Message + payload carry the COUNT + tenant_id only — no name/phone.
    assert _SYNTH_NAME not in trig.message_text
    assert _SYNTH_PHONE not in trig.message_text
    assert trig.payload == {"degraded_write_count": 1}


# ===========================================================================
# Part C — registry-aware Detector-5 bigram name match
# ===========================================================================

def test_c_blob_name_match_helper():
    pytest.importorskip("psycopg")
    from orchestrator.alerts.triggers import _blob_has_registry_name

    registry = _synth_registry(_SYNTH_NAME)
    leaked = f"decision: spoke with {_SYNTH_NAME} about the order"
    clean = "decision: spoke with the owner about the order"
    assert _blob_has_registry_name(leaked, registry) is True
    assert _blob_has_registry_name(clean, registry) is False


# ===========================================================================
# Part D — runner inbound redaction helper
# ===========================================================================

def test_d_runner_envelope_helper_strips_body_and_name(monkeypatch):
    pytest.importorskip("pydantic")
    from orchestrator import runner

    # Force the registry build to FAIL (outage) so we exercise the fail-soft
    # split through the runner path: known name-keys → sentinel, body popped.
    def _boom(_tenant_id):  # noqa: ANN001
        raise RuntimeError("synthetic registry outage")

    monkeypatch.setattr(
        "orchestrator.privacy.customer_registry.make_name_registry", _boom
    )
    # Suppress the §B alert dispatch (no DB in this dep-less test).
    monkeypatch.setattr(
        "orchestrator.observability.pipeline_observability."
        "_fire_registry_unavailable_alert",
        lambda *_a, **_k: None,
    )

    envelope = {
        "body": "sensitive plaintext from the owner",
        "customer_name": _SYNTH_NAME,
        "profile_name": _SYNTH_NAME,  # a NON-popped non-key field
        "twilio_message_sid": "SMtest0123456789abcdef0123456789ab",
        "sender_phone": "phone_tok_TEST",
    }
    safe = runner._redact_envelope_for_persistence(_SYNTH_TENANT, envelope)
    # Body popped (CL-390 belt preserved).
    assert "body" not in safe
    # Known name-key → registry-down sentinel.
    assert safe["customer_name"] == _REGISTRY_DOWN_SENTINEL
    # Provenance SID preserved.
    assert safe["twilio_message_sid"] == "SMtest0123456789abcdef0123456789ab"
    # The original dict is not mutated.
    assert envelope["body"] == "sensitive plaintext from the owner"


# ===========================================================================
# Synthetic-outage CANARY (Rule #15) — B + the cross-cutting PII boundary
# ===========================================================================

def test_outage_canary_known_keys_stripped_no_name_leaks(monkeypatch):
    """CANARY: simulate a registry OUTAGE end-to-end through write_step's
    redaction stage and assert the fail-soft split + the PII boundary.

    (1) known name-key fields stripped to <name:registry_down>;
    (2) the §A counter bumped build_error + pattern_only;
    (3) NO synthetic name/phone value reaches the redacted output.

    We drive ``_registry_for_tenant_with_status`` directly (the seam write_step
    uses) so this stays dep-less — no DB, no DBOS — while exercising the exact
    outage branch. Alert dispatch is stubbed (DB-gated; the live alert canary
    runs in the parent).
    """
    pytest.importorskip("pydantic")
    from orchestrator.observability import pipeline_observability as po
    from orchestrator.observability.pii import redact_for_log

    fired = {"count": 0}
    monkeypatch.setattr(
        po, "_fire_registry_unavailable_alert", lambda *_a, **_k: fired.__setitem__("count", fired["count"] + 1)
    )

    # Force the registry build to raise — the OUTAGE.
    def _boom(_tenant_id):  # noqa: ANN001
        raise RuntimeError("synthetic registry outage (canary)")

    monkeypatch.setattr(
        "orchestrator.privacy.customer_registry.make_name_registry", _boom
    )

    # The seam write_step calls when no registry is injected.
    registry, registry_down = po._registry_for_tenant_with_status(_SYNTH_TENANT)
    assert registry is None
    assert registry_down is True
    # (2) §A counter: build_error bumped by the seam.
    assert rh.registry_outcome_count(_SYNTH_TENANT, rh.RegistryOutcome.BUILD_ERROR) == 1
    # §B alert fired (stubbed).
    assert fired["count"] == 1

    # A synthetic record carrying a real-LOOKING name + phone across (i) two
    # KNOWN name keys, (ii) a phone in free text, (iii) a known-key phone.
    record = {
        "customer_name": _SYNTH_NAME,
        "owner_name": _SYNTH_OWNER,
        "phone": _SYNTH_PHONE,  # known phone key
        "decision_rationale": f"owner asked us to call {_SYNTH_PHONE}",  # free-text phone
    }
    safe = redact_for_log(record, name_registry=registry, registry_down=registry_down)
    # mode counter would be bumped by write_step; emulate that call here.
    po._record_redaction_mode(_SYNTH_TENANT, registry is not None)

    # (1) known name-keys stripped to the sentinel WITHOUT a registry.
    assert safe["customer_name"] == _REGISTRY_DOWN_SENTINEL
    assert safe["owner_name"] == _REGISTRY_DOWN_SENTINEL
    # (3) NO synthetic NAME value reaches the known-key fields (the high-confidence
    # surface the split CLOSES). The free-text-name residual is the accepted,
    # now-ALERTED gap (covered by §B above + Part C sweep) — so we assert closure
    # on the known-key surface + on ALL pattern-shaped PII everywhere.
    blob = repr(safe)
    assert _SYNTH_NAME not in blob, f"known-key name leaked: {safe!r}"
    assert _SYNTH_OWNER not in blob, f"known-key owner name leaked: {safe!r}"
    # Phone (pattern-shaped) redacted in BOTH the known key AND free text.
    assert _SYNTH_PHONE not in blob, f"phone leaked: {safe!r}"
    assert safe["phone"].startswith("phone_tok_")
    # The §A mode counter recorded pattern_only (registry was None).
    assert rh.redaction_mode_count(_SYNTH_TENANT, rh.RedactionMode.PATTERN_ONLY) == 1
    assert rh.redaction_mode_count(_SYNTH_TENANT, rh.RedactionMode.FULL) == 0


def test_outage_canary_free_text_name_residual_is_documented_gap(monkeypatch):
    """The accepted, ALERTED residual: during an outage a 2-token customer name
    in FREE TEXT (no known key, no digit/structure) is NOT redacted by the
    pattern layer. This test PINS that documented behaviour so a future change
    that silently widens or closes it is visible — the residual's safety net is
    §B's alert (it fires) + Part C's recovered-registry sweep, NOT silence.
    """
    from orchestrator.privacy.pii_redactor import redact

    # No registry, outage: free-text name is the residual.
    out = redact({"note": f"spoke with {_SYNTH_NAME}"}, registry_down=True)
    # Documented gap: the free-text name survives the OUTAGE pattern pass...
    assert _SYNTH_NAME in out["note"]
    # ...but WITH a registry it WOULD be masked (Part C / normal path), proving
    # the residual is a registry-availability gap, not a redactor blind spot.
    masked = redact(
        {"note": f"spoke with {_SYNTH_NAME}"},
        name_registry=_synth_registry(_SYNTH_NAME),
    )
    assert _SYNTH_NAME not in masked["note"]
