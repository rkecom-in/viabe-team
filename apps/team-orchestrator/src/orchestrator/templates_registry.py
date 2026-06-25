"""Approved-templates registry (VT-163).

Single resolver: ``(template_name, language)`` → ``TemplateEntry`` carrying
the Twilio Content SID + variable signature for that language variant.

Design decisions (Cowork-ruled 2026-05-30):
- D1: BOTH legacy yaml consumers (output_composer.load_twilio_templates +
  twilio_send._templates) are migrated onto this resolver in VT-163.
  No dual-representation; one yaml shape; one source of truth.
- D3: 60s TTL timestamped cache (NOT bare lru_cache) so a yaml data edit
  (e.g. a Hindi SID drop) is picked up without a process restart.
- D4: Typed error hierarchy — TemplateRegistryError base +
  UnknownTemplateError, UnknownLanguageVariantError,
  VariableSignatureMismatchError. TemplateNotConfigured alias for
  twilio_send back-compat.
- D5: approved_template_names(language) returns ONLY agent_selectable
  templates. Phase-1 ruling: only team_weekly_approval is agent-selectable.

CL-390 compliance: resolver logs template_name + language ONLY. Never logs
SIDs alongside params or any PII.
CL-422 compliance: config only, no customer data.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger(__name__)

_TEMPLATES_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "twilio_templates.yaml"
)

_SID_RE = re.compile(r"^HX[0-9a-f]{32}$")

_CACHE_TTL_SECONDS = 60.0

# Module-level cache: (loaded_at_monotonic, parsed_data)
# parsed_data maps template_name -> raw yaml dict entry.
_cache: tuple[float, dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# Typed error hierarchy
# ---------------------------------------------------------------------------


class TemplateRegistryError(ValueError):
    """Base for all registry errors."""


class UnknownTemplateError(TemplateRegistryError):
    """Raised when template_name is not in twilio_templates.yaml."""

    def __init__(self, name: str) -> None:
        self.template_name = name
        super().__init__(f"template '{name}' not in twilio_templates.yaml")


class UnknownLanguageVariantError(TemplateRegistryError):
    """Raised when template exists but the requested language variant is absent.

    Expected case during Phase 1: Hindi SIDs are not yet configured.
    """

    def __init__(self, name: str, language: str) -> None:
        self.template_name = name
        self.language = language
        super().__init__(
            f"template '{name}' has no '{language}' language variant in twilio_templates.yaml; "
            f"drop the SID under languages.{language} to activate it (data-only change)"
        )


class VariableSignatureMismatchError(TemplateRegistryError):
    """Raised when params supplied to validate_params don't match the signature.

    ``expected`` = frozenset of required param names from the yaml.
    ``got`` = frozenset of param names actually supplied.
    """

    def __init__(self, name: str, expected: frozenset[str], got: frozenset[str]) -> None:
        self.template_name = name
        self.expected = expected
        self.got = got
        missing = expected - got
        extra = got - expected
        parts = []
        if missing:
            parts.append(f"missing={sorted(missing)}")
        if extra:
            parts.append(f"extra={sorted(extra)}")
        super().__init__(
            f"template '{name}' variable signature mismatch: {'; '.join(parts)}"
        )


# Back-compat alias for twilio_send callers that catch TemplateNotConfigured.
TemplateNotConfigured = UnknownTemplateError


# ---------------------------------------------------------------------------
# TemplateEntry — resolved record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateEntry:
    """Resolved registry entry for one template × language combination.

    ``variables`` is an ordered tuple of snake_case param names;
    index i corresponds to positional ``{{i+1}}`` in the WhatsApp template.

    VT-369 Gap-5 fields (additive, optional in yaml — every pre-existing entry
    parses unchanged with the defaults below; MED-1):
    - ``category``: ``'customer_marketing'`` (agent → customer; Meta MARKETING)
      or ``'owner_notification'`` (agent → owner ops surface). ``""`` =
      uncategorised legacy entry — the agent customer-send gate
      (``agents/customer_send.py``) refuses anything that is not exactly
      ``'customer_marketing'``, so legacy entries fail that gate CLOSED.
    - ``money_bearing``: the body carries a discount/offer — trips the
      always-confirm floor at L3 (plan §5.5, enforced PR-3).
    - ``optout_line``: asserts the FIXED Meta body carries the customer STOP
      opt-out line; ``customer_marketing`` entries MUST pin it (canary_load).
    - ``body_sha256``: the sha256 of the Meta-APPROVED template body for this
      language, fetched once via the Twilio Content API at SID-wiring time
      (VT-383). ``None`` for pending-approval (null-SID) stubs. When pinned it
      is the doc/yaml/Meta drift detector — CI fails if the approved body moves.
    """

    template_name: str
    language: str
    content_sid: str | None  # None = configured but pending Meta approval
    audience: str
    variables: tuple[str, ...] = field(default_factory=tuple)
    agent_selectable: bool = False
    category: str = ""
    money_bearing: bool = False
    optout_line: bool = False
    body_sha256: str | None = None  # per-language APPROVED-body hash pin (VT-383)
    # VT-426 hardening: per-template "Fazal has approved this for LIVE owner sends"
    # gate. Defaults FALSE when absent in the yaml — a template the registry knows
    # about (real SID present) still does NOT send via _owner_notify until Fazal
    # flips this flag. The flag is read ONLY by the trial-sweep owner-notify path;
    # every other send path is unaffected (a missing key is backward-safe — all
    # pre-existing entries keep their existing behaviour on those paths).
    approved_for_live: bool = False


# Known values for the Gap-5 ``category`` field (canary_load rejects others).
TEMPLATE_CATEGORIES = frozenset({"customer_marketing", "owner_notification"})

# A pinned body_sha256 is a lowercase 64-char hex digest.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Internal yaml loader with TTL cache
# ---------------------------------------------------------------------------


def _load_raw(path: Path | None = None) -> dict[str, Any]:
    """Parse twilio_templates.yaml. No TTL logic; pure I/O."""
    p = path or _TEMPLATES_PATH
    if not p.exists():
        raise FileNotFoundError(f"twilio_templates.yaml not found at {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise TemplateRegistryError(
            f"twilio_templates.yaml must be a mapping; got {type(data).__name__}"
        )
    return data


def _get_cached(path: Path | None = None) -> dict[str, Any]:
    """Return cached yaml data, reloading if the TTL has expired.

    Uses module-level ``_cache`` tuple ``(loaded_at_monotonic, data)``.
    Thread-safety: GIL protects the tuple swap in CPython; acceptable for
    this use case (rare reload, no cross-process coordination needed).
    """
    global _cache
    now = time.monotonic()
    if _cache is not None:
        loaded_at, data = _cache
        if now - loaded_at < _CACHE_TTL_SECONDS:
            return data
    data = _load_raw(path)
    _cache = (now, data)
    return data


def _invalidate_cache() -> None:
    """Force next call to _get_cached() to reload from disk. Test helper."""
    global _cache
    _cache = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve(
    template_name: str,
    language: str,
    *,
    _path: Path | None = None,
) -> TemplateEntry:
    """Resolve ``(template_name, language)`` → ``TemplateEntry``.

    Raises:
        UnknownTemplateError: template_name not in yaml.
        UnknownLanguageVariantError: template exists, language variant absent.

    CL-390: only template_name + language appear in log lines.
    """
    data = _get_cached(_path)
    raw = data.get(template_name)
    if raw is None:
        logger.debug("registry: unknown template '%s'", template_name)
        raise UnknownTemplateError(template_name)

    langs: dict[str, Any] = raw.get("languages") or {}
    if language not in langs:
        logger.debug(
            "registry: template '%s' has no language variant '%s'",
            template_name,
            language,
        )
        raise UnknownLanguageVariantError(template_name, language)

    content_sid = langs[language]  # may be None (pending approval)
    variables = tuple(raw.get("variables") or [])
    audience = str(raw.get("audience") or "")
    agent_selectable = bool(raw.get("agent_selectable", False))

    sha_map = raw.get("body_sha256") or {}
    body_sha256 = sha_map.get(language) if isinstance(sha_map, dict) else None

    return TemplateEntry(
        template_name=template_name,
        language=language,
        content_sid=content_sid,
        audience=audience,
        variables=variables,
        agent_selectable=agent_selectable,
        category=str(raw.get("category") or ""),
        money_bearing=bool(raw.get("money_bearing", False)),
        optout_line=bool(raw.get("optout_line", False)),
        body_sha256=body_sha256,
        # VT-426: default FALSE when the key is absent — fail-closed for live sends.
        approved_for_live=bool(raw.get("approved_for_live", False)),
    )


def validate_params(
    template_name: str,
    language: str,
    params: dict[str, Any],
    *,
    _path: Path | None = None,
) -> None:
    """Assert that ``params`` keys match the template's variable signature.

    Raises:
        UnknownTemplateError: template not found.
        UnknownLanguageVariantError: language variant absent.
        VariableSignatureMismatchError: params keys don't match variables list.

    CL-390: only template_name + language in log lines; param values/keys
    are NOT logged (param names in the exception message are schema names,
    not customer PII).
    """
    entry = resolve(template_name, language, _path=_path)
    expected = frozenset(entry.variables)
    got = frozenset(params.keys())
    if expected != got:
        raise VariableSignatureMismatchError(template_name, expected, got)


def approved_template_names(
    language: str,
    *,
    _path: Path | None = None,
) -> tuple[str, ...]:
    """Return names of agent-selectable templates for the given language.

    Only templates with ``agent_selectable: true`` AND a configured SID for
    ``language`` are included. Phase-1: ``["team_weekly_approval"]`` only.

    Called at request-time (not import-time) so the 60s TTL cache applies —
    a yaml data edit (e.g. new agent_selectable entry) is picked up without
    a restart.
    """
    data = _get_cached(_path)
    names: list[str] = []
    for name, raw in data.items():
        if not isinstance(raw, dict):
            continue
        if not raw.get("agent_selectable", False):
            continue
        langs = raw.get("languages") or {}
        if language in langs:
            names.append(name)
    return tuple(names)


def content_sid_for(template_name: str, language: str = "en") -> str | None:
    """Convenience: resolve and return content_sid for a name+language.

    Returns None when the template is configured but pending Meta approval
    (content_sid: null in yaml). Raises UnknownTemplateError /
    UnknownLanguageVariantError for missing entries.

    Used by migrated output_composer and twilio_send callers.
    """
    return resolve(template_name, language).content_sid


# ---------------------------------------------------------------------------
# Canary (Rule #15)
# ---------------------------------------------------------------------------


def canary_load(path: Path | None = None) -> None:
    """Structural integrity check against the real on-disk yaml (Rule #15).

    Called at orchestrator startup. Fail-not-skip: any missing/malformed SID
    or absent variable signature raises TemplateRegistryError and aborts boot.

    Checks:
    - Every template entry is a mapping.
    - Every template has a non-empty ``variables`` list with unique snake_case names.
    - Every declared language variant has a content_sid matching ^HX[0-9a-f]{32}$
      (null/None SIDs are accepted as pending-approval stubs, not as errors).
    - No duplicate template names (yaml parser deduplicates; we verify the count).

    CL-390: never logs SID values; only template_name + language in log lines.
    CL-422: config check only; no customer data.
    """
    data = _load_raw(path)
    errors: list[str] = []

    for name, raw in data.items():
        if not isinstance(raw, dict):
            errors.append(f"  [{name}] entry is not a mapping")
            continue

        # variables check
        variables = raw.get("variables")
        if not variables or not isinstance(variables, list) or len(variables) == 0:
            errors.append(f"  [{name}] variables is missing or empty")
        else:
            unique = set(variables)
            if len(unique) != len(variables):
                errors.append(f"  [{name}] variables contains duplicates: {variables}")

        # Gap-5 field checks (VT-369, MED-1): additive optional fields — absent
        # is fine (legacy entry); present must be well-formed and a
        # customer_marketing entry MUST pin the opt-out line (plan §3b).
        category = raw.get("category")
        if category is not None and category not in TEMPLATE_CATEGORIES:
            errors.append(
                f"  [{name}] category {category!r} not in {sorted(TEMPLATE_CATEGORIES)}"
            )
        for flag in ("money_bearing", "optout_line", "approved_for_live"):
            if flag in raw and not isinstance(raw[flag], bool):
                errors.append(f"  [{name}] {flag} must be a bool")
        if category == "customer_marketing" and raw.get("optout_line") is not True:
            errors.append(
                f"  [{name}] customer_marketing templates MUST pin optout_line: true "
                "(the STOP line lives in the FIXED Meta body — plan §3b)"
            )

        # languages check
        langs = raw.get("languages")
        if langs is None:
            errors.append(f"  [{name}] languages block is missing")
            continue
        if not isinstance(langs, dict) or len(langs) == 0:
            errors.append(f"  [{name}] languages must be a non-empty mapping")
            continue

        for lang, sid in langs.items():
            if sid is None:
                # Pending-approval stub — accepted.
                continue
            if not isinstance(sid, str):
                errors.append(f"  [{name}][{lang}] content_sid is not a string: {type(sid).__name__}")
                continue
            if not _SID_RE.match(sid):
                errors.append(
                    f"  [{name}][{lang}] content_sid does not match ^HX[0-9a-f]{{32}}$"
                )

        # body_sha256 pin check (VT-383): optional. When present it must be a
        # mapping of language -> 64-hex digest, every pinned language must be a
        # declared variant, and a hash is forbidden on a null-SID stub (it would
        # pin the doc draft, not the Meta-APPROVED body — plan §3c).
        sha_map = raw.get("body_sha256")
        if sha_map is not None:
            if not isinstance(sha_map, dict) or len(sha_map) == 0:
                errors.append(f"  [{name}] body_sha256 must be a non-empty mapping")
            else:
                for lang, digest in sha_map.items():
                    if lang not in langs:
                        errors.append(
                            f"  [{name}][{lang}] body_sha256 pins an undeclared language"
                        )
                        continue
                    if not isinstance(digest, str) or not _SHA256_RE.match(digest):
                        errors.append(
                            f"  [{name}][{lang}] body_sha256 must be a 64-char lowercase hex digest"
                        )
                    if langs.get(lang) is None:
                        errors.append(
                            f"  [{name}][{lang}] body_sha256 pinned with no SID — "
                            "a hash on a null-SID stub pins the doc draft, not the "
                            "Meta-APPROVED body (plan §3c)"
                        )

    if errors:
        msg = "templates_registry canary_load FAILED:\n" + "\n".join(errors)
        raise TemplateRegistryError(msg)

    logger.info(
        "templates_registry canary_load OK: %d templates validated",
        len(data),
    )


__all__ = [
    "TEMPLATE_CATEGORIES",
    "TemplateEntry",
    "TemplateRegistryError",
    "TemplateNotConfigured",
    "UnknownTemplateError",
    "UnknownLanguageVariantError",
    "VariableSignatureMismatchError",
    "approved_template_names",
    "canary_load",
    "content_sid_for",
    "resolve",
    "validate_params",
]
