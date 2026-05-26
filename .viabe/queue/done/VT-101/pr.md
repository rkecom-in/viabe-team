---
pr_url: https://github.com/rkecom-in/viabe-team/pull/56
branch: feat/vt-observability-langsmith
sha: e979380
opened_at: 2026-05-25T20:34:00+05:30
---

# PR #56 — VT-101

URL: https://github.com/rkecom-in/viabe-team/pull/56
Title: `feat(observability): LangSmith + run_id propagation (VT-101)`
Branch: `feat/vt-observability-langsmith` → `main`
Commit: `e979380`

## 3-line summary

New `orchestrator/observability/` module wires LangSmith tracing using the existing `run_id: UUID` as the canonical trace ID (Pillar 8 by reuse). Inline `redact_for_langsmith()` at the decorator boundary — PII bypass mechanically blocked; VT-104 swap-in stays call-site stable. 19 pytest cases (all 6 brief acceptance criteria), mypy --strict clean, no regressions in the 198-test orchestrator suite.
