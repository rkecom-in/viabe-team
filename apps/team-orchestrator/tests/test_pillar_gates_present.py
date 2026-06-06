"""VT-107 — guard the CI Pillar gates from SILENT removal.

The edge-case coverage manifest (docs/edge-case-coverage-manifest.md) leans on 11 structural
`gate-no-*` / `gate-*` jobs in .github/workflows/ci.yml as the enforcement layer for several
failure-mode categories (Pillar 1 no-LLM-in-deterministic, Pillar 7 no-price-literals, RLS
no-direct-tenant-db, …). A gate deleted by accident would silently drop that enforcement and the
manifest would over-state coverage. This test fails if any expected gate job disappears.

Dep-less on purpose (only pathlib + str) so it runs in the pre-push dep-less smoke + the CI `test`
job. Adding/removing a gate is a deliberate act — update this list in the same PR (the test is the
forcing function), and reflect it in the manifest.
"""

from __future__ import annotations

from pathlib import Path

# The 11 structural Pillar gates the manifest maps (VT-107). Keep in sync with ci.yml + the manifest.
_EXPECTED_GATES = (
    "gate-no-deprecated-langgraph-imports",
    "gate-no-price-literals",
    "gate-no-llm-in-deterministic-triggers",
    "gate-no-langsmith-imports",
    "gate-no-direct-tenant-db-access",
    "gate-sr-agent-prompt-token-cap",
    "gate-vt39-tools-harness-import",
    "gate-connector-registry-schema",
    "gate-vt35-hard-limit-constants",
    "gate-langgraph-nodes-have-observability-hook",
    "gate-mcp-tools-have-observability-decorator",
)


def _ci_yml() -> Path:
    # tests/ → team-orchestrator → apps → repo root → .github/workflows/ci.yml
    return Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"


def test_ci_yml_exists() -> None:
    assert _ci_yml().is_file(), f"ci.yml not found at {_ci_yml()}"


def test_all_pillar_gates_present() -> None:
    """Every expected `gate-*` job must still declare `name: <gate>` in ci.yml."""
    text = _ci_yml().read_text(encoding="utf-8")
    missing = [g for g in _EXPECTED_GATES if f"name: {g}" not in text]
    assert not missing, (
        f"Pillar gate job(s) missing from ci.yml: {missing}. If a gate was removed deliberately, "
        "drop it from _EXPECTED_GATES + the edge-case manifest in the same PR."
    )
