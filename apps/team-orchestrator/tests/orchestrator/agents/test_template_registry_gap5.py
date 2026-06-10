"""VT-369 Gap-5 — template-registry CI pin (plan §3c + MED-1).

Pure Python: no DB, no LLM, no network. Reads the REAL on-disk
``config/twilio_templates.yaml``.

Pins, in order:
  1. All 5 Gap-5 entries exist with a ``category`` field; both winbacks are
     ``customer_marketing``; the 3 owner surfaces are ``owner_notification``
     (so they can structurally never pass the customer-send gate #2).
  2. Compliance flags: both winbacks ``optout_line: true`` (STOP line lives in
     the FIXED Meta body); ``team_winback_offer`` is ``money_bearing: true``
     (always-confirm floor, never L3 auto-send); ``team_winback_simple`` is NOT
     money-bearing.
  3. Fail-closed pre-F1: every Gap-5 entry declares en+hi variants with NO SID
     (``null`` stub) — ``resolve()`` returns ``content_sid is None`` and the
     send path refuses with ``TemplateNotConfigured`` until the F1 SIDs land.
     Body-hash (``body_sha256``) pins land WITH the F1 SIDs, not before.
  4. MED-1 regression pin: the new ``category`` field is a shared-resolver
     schema change touching the live campaign path — every PRE-EXISTING
     template entry must still parse and resolve for every declared language
     variant, and ``canary_load()`` must still pass on the real yaml.
  5. The live VT-45 agent-selectable set is unchanged pre-F1: no null-SID
     Gap-5 winback leaks into ``approved_template_names()`` (the D5 surface).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


pytest.importorskip("yaml")
import yaml  # noqa: E402

import orchestrator.templates_registry as reg  # noqa: E402
from orchestrator.templates_registry import (  # noqa: E402
    TemplateEntry,
    approved_template_names,
    canary_load,
    resolve,
)


_REAL_YAML_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "twilio_templates.yaml"
)

_WINBACK_NAMES = ("team_winback_simple", "team_winback_offer")

_OWNER_SURFACE_NAMES = (
    "team_agent_draft_approval",
    "team_l3_presend_notice",
    "team_autonomy_offer",
)

_GAP5_NAMES = _WINBACK_NAMES + _OWNER_SURFACE_NAMES


@pytest.fixture(autouse=True)
def _fresh_registry_cache():
    """The registry TTL cache is module-global and not keyed by path —
    invalidate around each test so we always read the real on-disk yaml."""
    reg._invalidate_cache()
    yield
    reg._invalidate_cache()


@pytest.fixture(scope="module")
def raw_yaml() -> dict[str, Any]:
    with _REAL_YAML_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict) and data
    return data


# ---------------------------------------------------------------------------
# 1+2. The 5 Gap-5 entries: presence, category, compliance flags
# ---------------------------------------------------------------------------


def test_all_five_gap5_entries_exist(raw_yaml: dict[str, Any]) -> None:
    for name in _GAP5_NAMES:
        assert name in raw_yaml, f"missing Gap-5 template entry: {name}"
        assert isinstance(raw_yaml[name], dict), f"{name} entry is not a mapping"


def test_every_gap5_entry_carries_a_category(raw_yaml: dict[str, Any]) -> None:
    for name in _GAP5_NAMES:
        category = raw_yaml[name].get("category")
        assert isinstance(category, str) and category, (
            f"{name} must carry the Gap-5 `category` registry field (plan gate #2)"
        )


def test_winbacks_are_customer_marketing_with_optout_line(
    raw_yaml: dict[str, Any],
) -> None:
    for name in _WINBACK_NAMES:
        entry = raw_yaml[name]
        assert entry.get("category") == "customer_marketing", (
            f"{name} must be category=customer_marketing (send gate #2)"
        )
        assert entry.get("audience") == "customer"
        assert entry.get("optout_line") is True, (
            f"{name} must pin optout_line: true — the STOP line lives in the "
            "FIXED Meta body (plan §3b)"
        )


def test_winback_offer_is_money_bearing_simple_is_not(
    raw_yaml: dict[str, Any],
) -> None:
    assert raw_yaml["team_winback_offer"].get("money_bearing") is True, (
        "team_winback_offer must be money_bearing: true — always-confirm "
        "floor, never L3 auto-send (plan §5.5)"
    )
    assert raw_yaml["team_winback_simple"].get("money_bearing", False) is False


def test_owner_surfaces_are_owner_notification_never_marketing(
    raw_yaml: dict[str, Any],
) -> None:
    for name in _OWNER_SURFACE_NAMES:
        entry = raw_yaml[name]
        assert entry.get("category") == "owner_notification", (
            f"{name} must be category=owner_notification"
        )
        assert entry.get("category") != "customer_marketing"
        assert entry.get("audience") == "owner"
        assert entry.get("agent_selectable", False) is False, (
            f"{name} is system-invoked, never agent-selectable"
        )


# ---------------------------------------------------------------------------
# 3. Fail-closed pre-F1: en+hi declared, NO SIDs
# ---------------------------------------------------------------------------


def test_gap5_entries_declare_en_and_hi_with_no_sid(
    raw_yaml: dict[str, Any],
) -> None:
    for name in _GAP5_NAMES:
        langs = raw_yaml[name].get("languages")
        assert isinstance(langs, dict), f"{name} languages block missing"
        for lang in ("en", "hi"):
            assert lang in langs, f"{name} must declare the {lang} variant (F1 is en+hi)"
            assert langs[lang] is None, (
                f"{name}.{lang} must have NO SID until F1 lands (fail-closed "
                "stub). If an F1 SID just landed, this pin moves to body-hash "
                "assertions (plan §3c) — update deliberately, never delete."
            )


def test_gap5_entries_resolve_with_content_sid_none() -> None:
    """resolve() must succeed (the entry is registered) while returning
    content_sid=None — the shape the send path fail-closes on
    (TemplateNotConfigured at the Twilio boundary)."""
    for name in _GAP5_NAMES:
        for lang in ("en", "hi"):
            entry = resolve(name, lang, _path=_REAL_YAML_PATH)
            assert isinstance(entry, TemplateEntry)
            assert entry.content_sid is None, (
                f"{name}.{lang} resolved a SID pre-F1 — fail-closed pin broken"
            )
            assert entry.variables, f"{name} must declare a variable signature"


def test_no_body_hash_pinned_before_f1(raw_yaml: dict[str, Any]) -> None:
    """body_sha256 pins land WITH the F1 SIDs (fetched from the Twilio Content
    API against the Meta-APPROVED body). A hash with no SID would pin the doc
    draft, not the approved copy — forbidden by plan §3c."""
    for name in _GAP5_NAMES:
        assert "body_sha256" not in raw_yaml[name], (
            f"{name} pins a body hash before its F1 SID landed"
        )


# ---------------------------------------------------------------------------
# 4. MED-1 regression pin — pre-existing entries still parse/resolve
# ---------------------------------------------------------------------------


def test_every_template_entry_still_parses_and_resolves(
    raw_yaml: dict[str, Any],
) -> None:
    """The `category` field is a shared-resolver schema change (MED-1, touches
    the live campaign path): every entry — pre-existing AND new — must still
    resolve for every declared language variant."""
    for name, entry in raw_yaml.items():
        assert isinstance(entry, dict), f"{name} entry is not a mapping"
        langs = entry.get("languages") or {}
        assert langs, f"{name} has no language variants"
        for lang in langs:
            resolved = resolve(name, lang, _path=_REAL_YAML_PATH)
            assert resolved.template_name == name
            assert resolved.language == lang
            assert resolved.audience in ("customer", "owner"), (
                f"{name} audience must be customer|owner"
            )
            assert resolved.variables, f"{name} must declare variables"


def test_canary_load_passes_on_real_yaml() -> None:
    """Startup structural canary still accepts the registry — null Gap-5 SIDs
    are the documented pending-approval stub, not an error."""
    canary_load(_REAL_YAML_PATH)


# ---------------------------------------------------------------------------
# 5. Live D5 selectable set unchanged pre-F1
# ---------------------------------------------------------------------------


def test_null_sid_winbacks_not_in_live_selectable_set() -> None:
    """Pre-F1 the winbacks must NOT enter approved_template_names() — they are
    unsendable (no SID) and would pollute the live VT-45 prompt set. They flip
    agent_selectable: true together with the F1 SID drop."""
    for lang in ("en", "hi"):
        names = approved_template_names(lang, _path=_REAL_YAML_PATH)
        for name in _GAP5_NAMES:
            assert name not in names, (
                f"{name} leaked into the live agent-selectable set pre-F1"
            )
