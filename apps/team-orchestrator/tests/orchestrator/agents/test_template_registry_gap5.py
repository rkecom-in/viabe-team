"""VT-369 Gap-5 — template-registry CI pin (plan §3c + MED-1).

Pure Python: no DB, no LLM, no network. Reads the REAL on-disk
``config/twilio_templates.yaml``.

ARMED at F1 (VT-383 / CL-438, 2026-06-12): Fazal delivered the 10 Meta-APPROVED
Content SIDs and the VT-383 canary confirmed every ``meta_status == approved``.
These pins moved from the fail-closed pre-F1 shape to the armed shape.

Pins, in order:
  1. All 5 Gap-5 entries exist with a ``category`` field; both winbacks are
     ``customer_marketing``; the 3 owner surfaces are ``owner_notification``
     (so they can structurally never pass the customer-send gate #2).
  2. Compliance flags: both winbacks ``optout_line: true`` (STOP line lives in
     the FIXED Meta body); ``team_winback_offer`` is ``money_bearing: true``
     (always-confirm floor, never L3 auto-send); ``team_winback_simple`` is NOT
     money-bearing. Both winbacks are now ``agent_selectable: true``; the 3
     owner surfaces stay ``agent_selectable: false`` (system-invoked).
  3. Armed at F1: every Gap-5 entry declares en+hi variants, each with a real
     ``^HX[0-9a-f]{32}$`` SID and a per-language ``body_sha256`` 64-hex pin
     (fetched from the Twilio Content API against the Meta-APPROVED body).
     ``resolve()`` returns the real SID + the body hash.
  4. MED-1 regression pin: the ``category`` + ``body_sha256`` fields are
     shared-resolver schema changes touching the live campaign path — every
     PRE-EXISTING template entry must still parse and resolve for every declared
     language variant, and ``canary_load()`` must still pass on the real yaml.
  5. The live VT-45 agent-selectable set now includes the two winbacks (D5
     surface): they are sendable (real SID) and Meta-approved. The 3 owner
     surfaces must NOT appear in ``approved_template_names()``.
"""

from __future__ import annotations

import re
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

_SID_RE = re.compile(r"^HX[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

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
        # VT-383: armed at F1 — Meta-approved + SIDs wired → agent-selectable.
        assert entry.get("agent_selectable") is True, (
            f"{name} must be agent_selectable: true at F1 (VT-383/CL-438) — "
            "Meta-approved with real SIDs, belongs in the live selectable set"
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
# 3. Armed at F1: en+hi declared, real HX SIDs, per-language body_sha256 pins
# ---------------------------------------------------------------------------


def test_gap5_entries_declare_en_and_hi_with_real_sid(
    raw_yaml: dict[str, Any],
) -> None:
    """VT-383: every Gap-5 entry declares en+hi, each with a real
    ^HX[0-9a-f]{32}$ SID (the fail-closed null stub is gone — F1 landed)."""
    for name in _GAP5_NAMES:
        langs = raw_yaml[name].get("languages")
        assert isinstance(langs, dict), f"{name} languages block missing"
        for lang in ("en", "hi"):
            assert lang in langs, f"{name} must declare the {lang} variant (F1 is en+hi)"
            sid = langs[lang]
            assert isinstance(sid, str) and _SID_RE.match(sid), (
                f"{name}.{lang} must carry a real ^HX[0-9a-f]{{32}}$ SID at F1 "
                f"(VT-383/CL-438); got {sid!r}"
            )


def test_gap5_entries_resolve_with_real_content_sid() -> None:
    """resolve() returns the real Meta-approved SID at F1 — the send path is
    live for these names (no longer fail-closed on null)."""
    for name in _GAP5_NAMES:
        for lang in ("en", "hi"):
            entry = resolve(name, lang, _path=_REAL_YAML_PATH)
            assert isinstance(entry, TemplateEntry)
            assert entry.content_sid is not None and _SID_RE.match(entry.content_sid), (
                f"{name}.{lang} did not resolve a real SID post-F1: {entry.content_sid!r}"
            )
            assert entry.variables, f"{name} must declare a variable signature"


def test_gap5_body_hash_pinned_per_language(raw_yaml: dict[str, Any]) -> None:
    """VT-383: each Gap-5 entry pins a per-language body_sha256 (64-char lowercase
    hex) fetched from the Twilio Content API against the Meta-APPROVED body. This
    is the doc/yaml/Meta drift detector (plan §3c). resolve() surfaces the hash."""
    for name in _GAP5_NAMES:
        sha_map = raw_yaml[name].get("body_sha256")
        assert isinstance(sha_map, dict), (
            f"{name} must pin body_sha256 per language at F1 (VT-383)"
        )
        for lang in ("en", "hi"):
            assert lang in sha_map, f"{name}.body_sha256 missing the {lang} pin"
            digest = sha_map[lang]
            assert isinstance(digest, str) and _SHA256_RE.match(digest), (
                f"{name}.body_sha256.{lang} must be 64-char lowercase hex; got {digest!r}"
            )
            # resolve() must surface the same per-language hash.
            entry = resolve(name, lang, _path=_REAL_YAML_PATH)
            assert entry.body_sha256 == digest, (
                f"{name}.{lang} resolve().body_sha256 != yaml pin"
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
            # VT-691: an EXPLICIT empty variables list is valid for static-body in-session
            # interactive objects (team_signup_consent_buttons — no {{n}} slots); a MISSING
            # declaration is still an error (canary_load enforces the distinction).
            assert resolved.variables is not None and isinstance(resolved.variables, tuple | list), (
                f"{name} must declare variables (an explicit empty list is allowed)"
            )


def test_canary_load_passes_on_real_yaml() -> None:
    """Startup structural canary still accepts the registry — null Gap-5 SIDs
    are the documented pending-approval stub, not an error."""
    canary_load(_REAL_YAML_PATH)


# ---------------------------------------------------------------------------
# 5. Live D5 selectable set — armed at F1
# ---------------------------------------------------------------------------


def test_winbacks_in_live_selectable_set_owner_surfaces_not() -> None:
    """VT-383: at F1 the two winbacks ARE sendable (real SID) + Meta-approved, so
    they enter approved_template_names() (the live VT-45 prompt set), in both en
    and hi. The 3 owner_notification surfaces are system-invoked and must NEVER
    appear there."""
    for lang in ("en", "hi"):
        names = approved_template_names(lang, _path=_REAL_YAML_PATH)
        for name in _WINBACK_NAMES:
            assert name in names, (
                f"{name} must be in the live agent-selectable set at F1 ({lang})"
            )
        for name in _OWNER_SURFACE_NAMES:
            assert name not in names, (
                f"{name} (owner_notification) must NOT be agent-selectable ({lang})"
            )
