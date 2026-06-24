"""VT-386 Part A — PII-free redaction health counters.

Process-local, monotonic counters that make customer-name-registry
outages and pattern-only redaction fallbacks COUNTABLE and ALERTABLE
without ever holding a name, phone, or field value.

PII boundary (HARD — CL-390 / CL-425 / CL-437)
----------------------------------------------
The keys of every counter are ``(tenant_id, enum-label)`` and the
values are integers. NO name / phone / body / registry contents ever
enter this module or its surface. The machinery that ANNOUNCES a leak
(the §B ``redaction_registry_unavailable`` alert, this §A health
surface) must itself be PII-free. Only ``tenant_id`` (an opaque UUID,
not customer PII) + the enum label + an integer count cross the
boundary.

What is counted
---------------
Two enum dimensions, both bounded:

- ``RegistryOutcome`` — the outcome of a per-tenant name-registry BUILD:
  ``ok`` · ``undefined_table`` (forward-compat "no customers table yet")
  · ``build_error`` (a real outage: bad grant, dropped table, pool
  starvation). Bumped at the two registry seams
  (``customer_registry.get_customer_names_for_tenant`` and
  ``pipeline_observability._registry_for_tenant``).

- ``RedactionMode`` — the redaction mode ACTUALLY applied per step write:
  ``full`` (a registry predicate was available) · ``pattern_only`` (the
  registry was ``None`` → names that aren't pattern-shaped leak). Bumped
  in ``write_step``.

Derived coverage metric (PII-free)
----------------------------------
``leak_exposure_ratio(tenant_id) = pattern_only / (full + pattern_only)``
— the fraction of step writes that fell back to pattern-only redaction
for a tenant in this process's lifetime. A nonzero ratio for a tenant
that HAS customers is the health red flag. It is a pure count ratio; it
carries no PII.

Scope / lifetime
----------------
Process-local (single-process Phase-1, same posture as
``customer_registry._CACHE``). Counts are monotonic within a process
and reset on restart — they are a health/exposure SIGNAL, not an audit
ledger (the audit substrate is the §B alert + the Detector-5 backstop).
A future VT row can persist these to a table without an API change.
Thread-safe via a module lock (DBOS step bodies + the FastAPI surface
can read/write concurrently).
"""

from __future__ import annotations

import threading
from enum import Enum


class RegistryOutcome(str, Enum):
    """Outcome enum for a per-tenant name-registry build. PII-free label."""

    OK = "ok"
    UNDEFINED_TABLE = "undefined_table"
    BUILD_ERROR = "build_error"


class RedactionMode(str, Enum):
    """Redaction mode actually applied to a step write. PII-free label."""

    FULL = "full"
    PATTERN_ONLY = "pattern_only"


# (tenant_id, label) -> monotonic count. Values are ints ONLY — never a value.
_REGISTRY_OUTCOMES: dict[tuple[str, str], int] = {}
_REDACTION_MODES: dict[tuple[str, str], int] = {}
_LOCK = threading.Lock()


def record_registry_outcome(tenant_id: str, outcome: RegistryOutcome) -> None:
    """Bump the (tenant_id, outcome) registry-build counter by 1.

    ``tenant_id`` is stringified at the seam; only it + the enum label are
    stored. No value, name, or registry content enters here.
    """
    key = (str(tenant_id), outcome.value)
    with _LOCK:
        _REGISTRY_OUTCOMES[key] = _REGISTRY_OUTCOMES.get(key, 0) + 1


def record_redaction_mode(tenant_id: str, mode: RedactionMode) -> None:
    """Bump the (tenant_id, mode) per-write redaction-mode counter by 1."""
    key = (str(tenant_id), mode.value)
    with _LOCK:
        _REDACTION_MODES[key] = _REDACTION_MODES.get(key, 0) + 1


def registry_outcome_count(tenant_id: str, outcome: RegistryOutcome) -> int:
    """Read one (tenant_id, outcome) registry-build count. 0 if unseen."""
    with _LOCK:
        return _REGISTRY_OUTCOMES.get((str(tenant_id), outcome.value), 0)


def redaction_mode_count(tenant_id: str, mode: RedactionMode) -> int:
    """Read one (tenant_id, mode) redaction-mode count. 0 if unseen."""
    with _LOCK:
        return _REDACTION_MODES.get((str(tenant_id), mode.value), 0)


def leak_exposure_ratio(tenant_id: str) -> float:
    """Return ``pattern_only / (full + pattern_only)`` for a tenant.

    The PII-free coverage metric: the fraction of this process's step
    writes for ``tenant_id`` that degraded to pattern-only redaction
    (names not pattern-shaped leaked). 0.0 when no writes have been
    recorded. A pure count ratio — carries no PII.
    """
    tid = str(tenant_id)
    with _LOCK:
        full = _REDACTION_MODES.get((tid, RedactionMode.FULL.value), 0)
        pattern_only = _REDACTION_MODES.get(
            (tid, RedactionMode.PATTERN_ONLY.value), 0
        )
    total = full + pattern_only
    if total == 0:
        return 0.0
    return pattern_only / total


def degraded_write_count(tenant_id: str) -> int:
    """Total registry-outage signal for a tenant in this process.

    ``build_error + undefined_table`` registry-build failures — the §B
    threshold input. ``undefined_table`` is included because a tenant that
    HAS customers but reads UndefinedTable is the same silent-leak shape
    (the customers table being absent for an active tenant is itself a
    misconfiguration the alert should surface). Counts only.
    """
    tid = str(tenant_id)
    with _LOCK:
        return (
            _REGISTRY_OUTCOMES.get((tid, RegistryOutcome.BUILD_ERROR.value), 0)
            + _REGISTRY_OUTCOMES.get((tid, RegistryOutcome.UNDEFINED_TABLE.value), 0)
        )


def snapshot() -> dict[str, dict[str, dict[str, int]]]:
    """PII-free health dump for the observability surface.

    Returns ``{"registry_outcomes": {tenant_id: {label: count}},
    "redaction_modes": {tenant_id: {label: count}}}`` — tenant_ids,
    enum labels, integer counts ONLY. No name / phone / value can appear
    here by construction (the counter stores can hold nothing else).
    """
    out_registry: dict[str, dict[str, int]] = {}
    out_modes: dict[str, dict[str, int]] = {}
    with _LOCK:
        for (tid, label), count in _REGISTRY_OUTCOMES.items():
            out_registry.setdefault(tid, {})[label] = count
        for (tid, label), count in _REDACTION_MODES.items():
            out_modes.setdefault(tid, {})[label] = count
    return {"registry_outcomes": out_registry, "redaction_modes": out_modes}


def reset() -> None:
    """Clear all counters (tests / process-wide reset)."""
    with _LOCK:
        _REGISTRY_OUTCOMES.clear()
        _REDACTION_MODES.clear()


__all__ = [
    "RedactionMode",
    "RegistryOutcome",
    "degraded_write_count",
    "leak_exposure_ratio",
    "record_redaction_mode",
    "record_registry_outcome",
    "redaction_mode_count",
    "registry_outcome_count",
    "reset",
    "snapshot",
]
