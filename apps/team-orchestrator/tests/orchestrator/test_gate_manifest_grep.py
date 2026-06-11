"""VT-374 F14 CI grep gate — every send/consent surface module is gate-manifest-listed.

The run-control registry refuses (at import time) to register a controllable step whose
implementing module is in ``run_control/gate_manifest.GATE_MODULES``. That guard is only
as good as the manifest's COMPLETENESS: a new send/consent module created outside the
manifest would be silently seam-eligible. This test is the completeness gate (the
Pillar-1 lint-test pattern — VT-72 ``check_no_direct_tenant_db_access`` / VT-365
``test_no_refund_subsystem``): it greps ``src/orchestrator`` for modules DEFINING
send/consent/approval surfaces and fails unless each one is in GATE_MODULES or in the
small justified allowlist below.

Dep-less by design (pathlib + re + importlib.util only) — this runs in the pre-push
dep-less smoke + the CI ``test`` job. ``gate_manifest.py`` is loaded DIRECTLY from its
file path (``spec_from_file_location``), never via ``import orchestrator.run_control...``:
a package import would execute ``run_control/__init__.py`` (the executor, which touches
DBOS) and break dep-less collection. Loading the manifest standalone also PROVES the
manifest module itself is stdlib-only — if it ever grows a third-party import, the load
here fails.

Adding a send/consent surface is a deliberate act: put the module in GATE_MODULES (it is
a gate) or — only with review — in ``_ALLOWLIST`` with a justification. The test is the
forcing function.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "orchestrator"
_MANIFEST_PATH = _SRC / "run_control" / "gate_manifest.py"

# A surface is a module that DEFINES one of these (def-anchored so call sites never
# trip the gate — consuming a send fn is fine; defining one makes you a surface).
_DEF = r"^[ \t]*(?:async[ \t]+)?def[ \t]+"
_SURFACE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("whatsapp send def", re.compile(_DEF + r"send_whatsapp\w*", re.MULTILINE)),
    ("template send def", re.compile(_DEF + r"send_template\w*", re.MULTILINE)),
    ("freeform send def", re.compile(_DEF + r"send_freeform\w*", re.MULTILINE)),
    ("agent customer-send gate", re.compile(_DEF + r"agent_send_\w+", re.MULTILINE)),
    (
        "consent helper",
        re.compile(
            _DEF
            + r"(?:record_consent\w*|has_consent\w*|has_marketing_consent\w*"
            + r"|opt_out\w*|purge_consent\w*)\b",
            re.MULTILINE,
        ),
    ),
    ("approval arming", re.compile(_DEF + r"arm_\w+", re.MULTILINE)),
    (
        "pre-filter / opt-out gate",
        re.compile(_DEF + r"(?:pre_filter|matches_opt_out_or_dsr)\b", re.MULTILINE),
    ),
    ("phase transition (money edges)", re.compile(_DEF + r"apply_transition\b", re.MULTILINE)),
)

# Justified residuals: surface-matching modules that are deliberately NOT manifest
# members. Every entry needs a reason; test_allowlist_entries_stay_live() forces
# removal once an entry stops matching (no stale exemptions) AND forbids any overlap
# with GATE_MODULES (the manifest is the binding arm — a gate module already covers
# the surface, so an allowlist twin is dead weight). All four original residuals
# (send_whatsapp_message, send_whatsapp_template, freeform_acks, opt_out_handler)
# were in fact ALREADY in GATE_MODULES, so they belong there, not here.
_ALLOWLIST: dict[str, str] = {}


def _load_gate_modules() -> frozenset[str]:
    """Load GATE_MODULES from gate_manifest.py WITHOUT importing the package chain."""
    assert _MANIFEST_PATH.is_file(), (
        f"run_control/gate_manifest.py missing at {_MANIFEST_PATH} — the F14 deny-list "
        "manifest is a VT-374 deliverable; this gate fails (never skips) without it."
    )
    spec = importlib.util.spec_from_file_location("_vt374_gate_manifest", _MANIFEST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Executes the manifest standalone: any non-stdlib import inside it raises HERE,
    # in the dep-less job — which is exactly the "manifest must stay dep-less" check.
    spec.loader.exec_module(module)
    gate_modules = module.GATE_MODULES
    assert isinstance(gate_modules, frozenset), "GATE_MODULES must be a frozenset"
    assert gate_modules, "GATE_MODULES must not be empty"
    assert all(isinstance(m, str) and m.startswith("orchestrator.") for m in gate_modules), (
        f"GATE_MODULES entries must be orchestrator.* dotted paths: {sorted(gate_modules)}"
    )
    return gate_modules


def _module_file(dotted: str) -> Path | None:
    """Resolve an orchestrator.* dotted path to its source file (module or package)."""
    rel = Path(*dotted.split(".")[1:])  # strip the leading 'orchestrator'
    candidate = _SRC / rel.with_suffix(".py")
    if candidate.is_file():
        return candidate
    package_init = _SRC / rel / "__init__.py"
    if package_init.is_file():
        return package_init
    return None


def _dotted(path: Path) -> str:
    rel = path.relative_to(_SRC)
    parts = ("orchestrator", *rel.parts[:-1], rel.stem)
    if rel.stem == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _surface_modules() -> dict[str, list[str]]:
    """Scan src/orchestrator: dotted module path -> matched pattern labels."""
    hits: dict[str, list[str]] = {}
    for py in sorted(_SRC.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        labels = [label for label, pattern in _SURFACE_PATTERNS if pattern.search(text)]
        if labels:
            hits[_dotted(py)] = labels
    return hits


def test_gate_manifest_loads_depless_and_modules_exist():
    """The manifest loads stdlib-only, and every listed module is a real source file
    (catches a rename silently leaving the manifest pointing at nothing)."""
    gate_modules = _load_gate_modules()
    missing = sorted(m for m in gate_modules if _module_file(m) is None)
    assert not missing, (
        f"GATE_MODULES entries with no source file under src/orchestrator: {missing} — "
        "a gate module was moved/renamed; update run_control/gate_manifest.py in the same PR."
    )


def test_send_consent_surfaces_covered_by_manifest_or_allowlist():
    """THE F14 gate: every module defining a send/consent/approval surface must be in
    GATE_MODULES (a gate) or in the justified _ALLOWLIST here."""
    gate_modules = _load_gate_modules()
    covered = gate_modules | set(_ALLOWLIST)
    violations = {
        module: labels
        for module, labels in _surface_modules().items()
        if module not in covered
    }
    assert not violations, (
        "send/consent surface modules OUTSIDE the gate manifest (VT-374 F14):\n"
        + "\n".join(f"  {m}: {labels}" for m, labels in sorted(violations.items()))
        + "\nAdd the module to run_control/gate_manifest.py GATE_MODULES (it is a gate "
        "the run-control registry must refuse), or — only with review — to the "
        "justified _ALLOWLIST in this test."
    )


def test_allowlist_entries_stay_live():
    """No stale exemptions: every allowlist entry must still exist, still match a
    surface pattern, and never shadow a manifest entry."""
    gate_modules = _load_gate_modules()
    overlap = sorted(set(_ALLOWLIST) & gate_modules)
    assert not overlap, (
        f"_ALLOWLIST entries duplicated in GATE_MODULES: {overlap} — the manifest wins; "
        "remove them from the allowlist."
    )
    surfaces = _surface_modules()
    stale = sorted(m for m in _ALLOWLIST if m not in surfaces)
    assert not stale, (
        f"stale _ALLOWLIST entries (file gone or no longer a surface): {stale} — "
        "remove them; exemptions must stay earned."
    )
