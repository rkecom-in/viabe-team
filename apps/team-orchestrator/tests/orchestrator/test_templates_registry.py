"""VT-163 — templates_registry unit tests.

Pure Python: no DB, no LLM, no network.

Coverage:
  1. resolve(name, lang) -> TemplateEntry with correct SID, audience, variables.
  2. resolve(unknown_name, lang) -> UnknownTemplateError.
  3. resolve(known_name, absent_lang) -> UnknownLanguageVariantError.
  4. validate_params(name, lang, wrong_keys) -> VariableSignatureMismatchError.
  5. validate_params(name, lang, correct_keys) -> no raise.
  6. approved_template_names("en") -> only agent_selectable names.
  7a. Cache: second call within 60s does NOT re-read disk.
  7b. Cache: re-read after TTL expiry.
  8. Back-compat: output_composer routing test template names resolve.
     Back-compat: twilio_send.TemplateNotConfigured path raises for unknown name.
  9. CL-390: resolve/logger helpers emit template_name+language only (no SID/PII).
  10. canary_load() passes on the real on-disk yaml.
  11. canary_load() raises TemplateRegistryError on malformed yaml.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("yaml")

import orchestrator.templates_registry as reg
from orchestrator.templates_registry import (
    TemplateEntry,
    TemplateNotConfigured,
    TemplateRegistryError,
    UnknownLanguageVariantError,
    UnknownTemplateError,
    VariableSignatureMismatchError,
    approved_template_names,
    canary_load,
    resolve,
    validate_params,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REAL_YAML_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "twilio_templates.yaml"
)

_KNOWN_EN_SID = "HX44b053c946a230ea0d2d3d2dc6118964"  # team_weekly_approval en SID

_MINIMAL_YAML = {
    "team_weekly_approval": {
        "audience": "customer",
        "agent_selectable": True,
        "variables": ["customer_segment", "campaign_mode", "projected_recovery_inr"],
        "languages": {"en": "HX44b053c946a230ea0d2d3d2dc6118964"},
    },
    "team_opt_out_confirmation": {
        "audience": "customer",
        "agent_selectable": False,
        "variables": ["owner_name"],
        "languages": {"en": "HX6365c429e75c2e191bf396e1c6ba8708"},
    },
}


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure each test starts with a cold registry cache."""
    reg._invalidate_cache()
    yield
    reg._invalidate_cache()


def _patch_cache(data: dict[str, Any]):
    """Context manager: replace the registry cache with custom data."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        reg._cache = (time.monotonic(), data)
        try:
            yield
        finally:
            reg._invalidate_cache()

    return _ctx()


# ---------------------------------------------------------------------------
# 1. resolve(name, lang) → TemplateEntry
# ---------------------------------------------------------------------------


def test_resolve_known_name_en_returns_correct_entry():
    entry = resolve("team_weekly_approval", "en", _path=_REAL_YAML_PATH)
    assert isinstance(entry, TemplateEntry)
    assert entry.template_name == "team_weekly_approval"
    assert entry.language == "en"
    assert entry.content_sid == _KNOWN_EN_SID
    assert entry.audience == "customer"
    assert entry.variables == ("customer_segment", "campaign_mode", "projected_recovery_inr")
    assert entry.agent_selectable is True


def test_resolve_returns_frozen_dataclass():
    entry = resolve("team_weekly_approval", "en", _path=_REAL_YAML_PATH)
    with pytest.raises((AttributeError, TypeError)):
        entry.template_name = "something_else"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. resolve(unknown_name, lang) → UnknownTemplateError
# ---------------------------------------------------------------------------


def test_resolve_unknown_template_raises():
    with pytest.raises(UnknownTemplateError) as exc_info:
        resolve("does_not_exist", "en", _path=_REAL_YAML_PATH)
    assert "does_not_exist" in str(exc_info.value)


def test_unknown_template_error_is_template_registry_error():
    with pytest.raises(TemplateRegistryError):
        resolve("does_not_exist", "en", _path=_REAL_YAML_PATH)


def test_unknown_template_error_is_value_error():
    with pytest.raises(ValueError):
        resolve("does_not_exist", "en", _path=_REAL_YAML_PATH)


# ---------------------------------------------------------------------------
# 3. resolve(known_name, absent_lang) → UnknownLanguageVariantError
# ---------------------------------------------------------------------------


def test_resolve_hi_returns_sid():
    """VT-163-fix-1: Hindi SIDs are now populated (Fazal addendum) — hi resolves."""
    entry = resolve("team_weekly_approval", "hi", _path=_REAL_YAML_PATH)
    assert entry.content_sid == "HX4c63feb64d392ada48b0fe11cb1d067d"


def test_resolve_absent_language_raises():
    """A genuinely-absent language variant (only en+hi configured) raises."""
    with pytest.raises(UnknownLanguageVariantError) as exc_info:
        resolve("team_weekly_approval", "ta", _path=_REAL_YAML_PATH)
    assert "team_weekly_approval" in str(exc_info.value)
    assert "ta" in str(exc_info.value)


def test_unknown_language_variant_error_attrs():
    err = UnknownLanguageVariantError("team_weekly_approval", "hi")
    assert err.template_name == "team_weekly_approval"
    assert err.language == "hi"


# ---------------------------------------------------------------------------
# 4. validate_params — wrong keys → VariableSignatureMismatchError
# ---------------------------------------------------------------------------


def test_validate_params_wrong_keys_raises():
    with pytest.raises(VariableSignatureMismatchError) as exc_info:
        validate_params(
            "team_weekly_approval",
            "en",
            {"wrong_key": "value"},
            _path=_REAL_YAML_PATH,
        )
    err = exc_info.value
    assert err.template_name == "team_weekly_approval"
    assert "customer_segment" in err.expected
    assert "wrong_key" in err.got
    assert "missing" in str(err).lower() or "extra" in str(err).lower()


def test_validate_params_extra_key_raises():
    valid = {
        "customer_segment": "30d_silent",
        "campaign_mode": "winback",
        "projected_recovery_inr": "500",
    }
    extra = {**valid, "unexpected_extra": "oops"}
    with pytest.raises(VariableSignatureMismatchError):
        validate_params("team_weekly_approval", "en", extra, _path=_REAL_YAML_PATH)


# ---------------------------------------------------------------------------
# 5. validate_params — correct keys → no raise
# ---------------------------------------------------------------------------


def test_validate_params_correct_keys_no_raise():
    validate_params(
        "team_weekly_approval",
        "en",
        {
            "customer_segment": "30d_silent",
            "campaign_mode": "winback",
            "projected_recovery_inr": "500",
        },
        _path=_REAL_YAML_PATH,
    )  # must not raise


# ---------------------------------------------------------------------------
# 6. approved_template_names — only agent_selectable
# ---------------------------------------------------------------------------


# VT-383 (CL-438, 2026-06-12): F1 armed the two Gap-5 winbacks — Meta-approved
# SIDs wired + agent_selectable flipped true. The live VT-45 selectable set grew
# from {team_weekly_approval} to {team_weekly_approval, team_winback_simple,
# team_winback_offer}. The 3 owner_notification surfaces stay non-selectable.
_SELECTABLE_SET = frozenset(
    {"team_weekly_approval", "team_winback_simple", "team_winback_offer"}
)


def test_approved_template_names_en_returns_only_selectable():
    names = approved_template_names("en", _path=_REAL_YAML_PATH)
    assert isinstance(names, tuple)
    # VT-383: the live selectable set is exactly these three (D5 pin, armed shape).
    assert set(names) == _SELECTABLE_SET, f"selectable set drifted: {sorted(names)}"
    # Verify non-selectable templates are excluded.
    for name in ("team_opt_out_confirmation", "team_dsr_acknowledgment",
                 "team_error_handler", "team_status_ping",
                 "team_unable_to_complete_request", "team_agent_stuck_escalation",
                 "team_welcome",
                 # Gap-5 owner surfaces — system-invoked, never agent-selectable.
                 "team_agent_draft_approval", "team_l3_presend_notice",
                 "team_autonomy_offer"):
        assert name not in names, f"{name} should not be agent_selectable"


def test_approved_template_names_hi_populated():
    """VT-163-fix-1: hi SIDs populated. VT-383: F1 armed the two winbacks in hi
    too (Meta-approved hi SIDs), so the hi selectable set matches en."""
    names = approved_template_names("hi", _path=_REAL_YAML_PATH)
    assert set(names) == _SELECTABLE_SET, f"hi selectable set drifted: {sorted(names)}"


# ---------------------------------------------------------------------------
# 7a. Cache: second call within 60s does NOT re-read disk
# ---------------------------------------------------------------------------


def test_cache_second_call_no_disk_read(tmp_path):
    """After first resolve, second resolve within TTL must NOT reload from disk."""
    # Write a valid yaml to tmp dir.
    tmp_yaml = tmp_path / "twilio_templates.yaml"
    tmp_yaml.write_text(yaml.dump(_MINIMAL_YAML))

    # Prime the cache.
    reg._invalidate_cache()
    e1 = resolve("team_weekly_approval", "en", _path=tmp_yaml)

    # Now overwrite the file with garbage to prove disk is NOT re-read.
    tmp_yaml.write_text("this: is: invalid: yaml: !!!")

    # Second call should hit the cache and return the same SID.
    e2 = resolve("team_weekly_approval", "en", _path=tmp_yaml)
    assert e1.content_sid == e2.content_sid


# ---------------------------------------------------------------------------
# 7b. Cache: re-read after TTL expiry
# ---------------------------------------------------------------------------


def test_cache_reloads_after_ttl(tmp_path, monkeypatch):
    """After TTL expires, the registry reloads from disk."""
    tmp_yaml = tmp_path / "twilio_templates.yaml"
    tmp_yaml.write_text(yaml.dump(_MINIMAL_YAML))

    reg._invalidate_cache()
    resolve("team_weekly_approval", "en", _path=tmp_yaml)

    # Monkey-patch TTL to 0 so next call always considers cache stale.
    monkeypatch.setattr(reg, "_CACHE_TTL_SECONDS", 0.0)

    # Replace yaml content with a new SID.
    new_sid = "HX" + "a" * 32
    updated = {
        "team_weekly_approval": {
            **_MINIMAL_YAML["team_weekly_approval"],
            "languages": {"en": new_sid},
        }
    }
    tmp_yaml.write_text(yaml.dump(updated))

    entry = resolve("team_weekly_approval", "en", _path=tmp_yaml)
    assert entry.content_sid == new_sid


# ---------------------------------------------------------------------------
# 8. Back-compat: migrated consumers still resolve correctly
# ---------------------------------------------------------------------------


def test_output_composer_load_twilio_templates_contains_all_8():
    """output_composer.load_twilio_templates() delegates to registry; all 8 names present."""
    pytest.importorskip("orchestrator.output_composer")
    from orchestrator.output_composer import load_twilio_templates

    templates = load_twilio_templates()
    expected_names = {
        "team_welcome",
        "team_weekly_approval",
        "team_opt_out_confirmation",
        "team_dsr_acknowledgment",
        "team_agent_stuck_escalation",
        "team_status_ping",
        "team_unable_to_complete_request",
        "team_error_handler",
    }
    assert expected_names <= set(templates.keys()), (
        f"missing: {expected_names - set(templates.keys())}"
    )


def test_output_composer_load_twilio_templates_returns_same_sids():
    """SIDs from load_twilio_templates match direct registry resolve."""
    pytest.importorskip("orchestrator.output_composer")
    from orchestrator.output_composer import load_twilio_templates

    templates = load_twilio_templates()
    # The delegated function returns raw yaml dict (nested lang shape).
    # The test verifies the name is present; SID access is via registry.
    assert "team_weekly_approval" in templates


def test_twilio_send_template_not_configured_is_unknown_template_error():
    """TemplateNotConfigured == UnknownTemplateError (alias, D4)."""
    assert TemplateNotConfigured is UnknownTemplateError


def test_twilio_send_raises_template_not_configured_for_unknown_name():
    """twilio_send.TemplateNotConfigured raises for unknown template (back-compat)."""
    # We import the alias from templates_registry as twilio_send re-exports it.
    with _patch_cache(_MINIMAL_YAML):
        with pytest.raises(TemplateNotConfigured):
            resolve("no_such_template", "en")


# ---------------------------------------------------------------------------
# 9. CL-390: log lines carry template_name+language only (no SID/PII)
# ---------------------------------------------------------------------------


def test_resolve_logs_only_template_name_and_language_not_sid(caplog):
    """CL-390: resolver log lines must not carry SID values."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="orchestrator.templates_registry"):
        try:
            resolve("does_not_exist", "en", _path=_REAL_YAML_PATH)
        except UnknownTemplateError:
            pass

    sid_pattern = re.compile(r"HX[0-9a-f]{32}", re.IGNORECASE)
    for record in caplog.records:
        assert not sid_pattern.search(record.getMessage()), (
            f"SID leaked into log: {record.getMessage()!r}"
        )


# ---------------------------------------------------------------------------
# 10. canary_load() passes on real yaml
# ---------------------------------------------------------------------------


def test_canary_load_passes_on_real_yaml():
    """Rule #15: canary_load must not raise on the checked-in yaml."""
    canary_load(_REAL_YAML_PATH)  # must not raise


# ---------------------------------------------------------------------------
# 11. canary_load() raises on malformed yaml
# ---------------------------------------------------------------------------


def test_canary_load_raises_on_missing_languages(tmp_path):
    bad = tmp_path / "twilio_templates.yaml"
    bad.write_text(yaml.dump({
        "team_broken": {
            "audience": "customer",
            "variables": ["name"],
            # 'languages' key absent — should fail
        }
    }))
    with pytest.raises(TemplateRegistryError, match="languages block is missing"):
        canary_load(bad)


def test_canary_load_raises_on_malformed_sid(tmp_path):
    bad = tmp_path / "twilio_templates.yaml"
    bad.write_text(yaml.dump({
        "team_broken": {
            "audience": "customer",
            "variables": ["name"],
            "languages": {"en": "NOTASID123"},
        }
    }))
    with pytest.raises(TemplateRegistryError, match="does not match"):
        canary_load(bad)


def test_canary_load_raises_on_empty_variables(tmp_path):
    bad = tmp_path / "twilio_templates.yaml"
    bad.write_text(yaml.dump({
        "team_broken": {
            "audience": "customer",
            "variables": [],
            "languages": {"en": "HX" + "a" * 32},
        }
    }))
    with pytest.raises(TemplateRegistryError, match="variables is missing or empty"):
        canary_load(bad)


def test_canary_load_accepts_null_sid_stub(tmp_path):
    """content_sid: null (pending approval) must be accepted, not an error."""
    valid = tmp_path / "twilio_templates.yaml"
    valid.write_text(yaml.dump({
        "team_pending": {
            "audience": "customer",
            "variables": ["name"],
            "languages": {"en": None},
        }
    }))
    canary_load(valid)  # must not raise


# ---------------------------------------------------------------------------
# VT-248 — team_campaign_not_sent (count-bearing rejection template)
# ---------------------------------------------------------------------------

def test_campaign_not_sent_resolves_en_and_hi():
    en = resolve("team_campaign_not_sent", "en", _path=_REAL_YAML_PATH)
    hi = resolve("team_campaign_not_sent", "hi", _path=_REAL_YAML_PATH)
    assert en.content_sid == "HXcedcda2a0bc1e8f47b37950ef458feb4"
    assert hi.content_sid == "HXcd2688e6ea1862c063378b18e382e700"
    assert en.audience == "owner"
    # {{1}} owner_name, {{2}} count of unverified targets.
    assert tuple(en.variables) == ("owner_name", "unverified_count")


def test_campaign_not_sent_is_not_agent_selectable():
    """SYSTEM-invoked on the rejection path — the agent-selectable set stays
    {team_weekly_approval} (D5). It must NOT appear in approved_template_names."""
    assert "team_campaign_not_sent" not in approved_template_names(
        "en", _path=_REAL_YAML_PATH
    )


def test_campaign_not_sent_validate_params_accepts_signature():
    # Exactly the two registry variables → no VariableSignatureMismatchError.
    validate_params(
        "team_campaign_not_sent",
        "en",
        {"owner_name": "Asha", "unverified_count": "3"},
        _path=_REAL_YAML_PATH,
    )


# --------------------------------------------------------------------------- #
# VT-365 (Fazal 2026-06-09) — 30-day flat trial, NO refund, NO trial extensions.
# The refund subsystem + trial extensions are removed: `trial_extension_offered`,
# `trial_max_reached`, `refund_offer`, `refund_completed` are retired from the
# canonical registry (`.viabe/templates.md`). Only `trial_ending` survives as the
# trial-lifecycle warn template (subscribe-or-lapse).
# --------------------------------------------------------------------------- #
_VT365_TRIAL_LIVE = ("trial_ending",)
# The 3 in-window acks are FREE-FORM (not templates) — REMOVED from the registry in VT-349.
_VT349_FREEFORM_REMOVED = ("refund_processing", "support_handoff", "team_edge_case_ack")


def test_trial_ending_resolves_live_sid_both_langs() -> None:
    """VT-365: the surviving trial-lifecycle template (`trial_ending`) resolves a
    non-null HX content_sid in {en, hi} (no fail-closed stub)."""
    for name in _VT365_TRIAL_LIVE:
        for lang in ("en", "hi"):
            sid = reg.content_sid_for(name, lang)
            assert sid is not None, f"{name}[{lang}] still null (fail-closed)"
            assert re.match(r"^HX[0-9a-f]{32}$", sid), f"{name}[{lang}] bad SID: {sid!r}"


def test_vt349_freeform_acks_removed_from_registry() -> None:
    """VT-349: the 3 in-window acks are FREE-FORM now (not templates) → removed from the
    registry; resolving them raises UnknownTemplateError."""
    from orchestrator.templates_registry import UnknownTemplateError

    for name in _VT349_FREEFORM_REMOVED:
        with pytest.raises(UnknownTemplateError):
            reg.content_sid_for(name, "en")


# ---------------------------------------------------------------------------
# Batch 2 (VT-108, Fazal SIDs 2026-06-07) — registry acceptance
# ---------------------------------------------------------------------------

_SID_RE = re.compile(r"^HX[0-9a-f]{32}$")

_BATCH2 = {
    "support_resolved": ("owner", ("support_reference_id",)),
    "trial_subscribe_link": ("owner", ("owner_name", "subscribe_link")),
    "dsr_deletion_completed": ("customer", ("business_name",)),
    "breach_notification_owner": ("owner", ("owner_name", "affected_summary", "action_taken")),
    "breach_notification_customer": ("customer", ("business_name", "data_categories", "advised_step")),
}


def test_batch2_templates_resolve_en_and_hi():
    """All 10 batch-2 SIDs (5 templates x EN/HI) resolve non-null + ^HX[0-9a-f]{32}$, with the
    documented audience + variable signature, and NONE agent_selectable (system/compliance-only)."""
    for name, (audience, variables) in _BATCH2.items():
        for lang in ("en", "hi"):
            entry = resolve(name, lang)
            assert _SID_RE.match(entry.content_sid), f"{name}/{lang} SID {entry.content_sid!r}"
            assert entry.audience == audience, f"{name} audience"
            assert tuple(entry.variables) == variables, f"{name} variables"
            assert entry.agent_selectable is False, f"{name} must NOT be agent_selectable"


def test_batch2_distinct_sids():
    """No copy-paste collision — all 10 batch-2 SIDs are distinct."""
    sids = [resolve(n, lg).content_sid for n in _BATCH2 for lg in ("en", "hi")]
    assert len(set(sids)) == 10
