#!/usr/bin/env python3
"""VT-72 — `no-direct-tenant-db-access` lint (Phase-1: report/allowlist mode).

Forbids NEW direct SQL access to the tenant-scoped hot tables outside the
wrapper layer. Existing accessor files are allowlisted (layer-1 RLS already
protects them); a NEW non-allowlisted file touching these tables fails the gate.

VT-306 owns the full call-site migration + flipping this to hard-fail (empty
allowlist). Until then this gate's job is purely to stop regressions.

Exit 0 = clean (only allowlisted files / the wrapper layer touch the tables).
Exit 1 = a non-allowlisted file directly accesses a tenant-scoped hot table.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Tenant-scoped hot tables wrapped in Phase-1 (db/wrappers/). Direct SQL against
# these outside the wrapper layer + the allowlist is a regression.
_TABLES = (
    "customers",
    "campaigns",
    "pending_approvals",
    "owner_inputs",
    "phone_token_resolutions",
)

# A table token appearing right after a SQL clause keyword = direct access.
_PATTERNS = {
    t: re.compile(rf"\b(?:FROM|INTO|UPDATE|JOIN)\s+{t}\b", re.IGNORECASE)
    for t in _TABLES
}

_SRC = Path("apps/team-orchestrator/src/orchestrator")

# The wrapper layer is the sanctioned access point.
_WRAPPER_DIR = _SRC / "db"

# Allowlist of EXISTING direct-access files (relative to repo root). Layer-1 RLS
# protects these; VT-306 migrates them to wrappers + empties this list. A NEW
# file touching the tables must NOT be added here — it must use a wrapper.
_ALLOWLIST = frozenset(
    {
        "apps/team-orchestrator/src/orchestrator/owner_surface/monthly_report.py",
        "apps/team-orchestrator/src/orchestrator/privacy/cohort.py",
        "apps/team-orchestrator/src/orchestrator/privacy/customer_registry.py",
        "apps/team-orchestrator/src/orchestrator/agent/tools/send_whatsapp_message.py",
        "apps/team-orchestrator/src/orchestrator/agent/tools/send_whatsapp_template.py",
        "apps/team-orchestrator/src/orchestrator/integrations/dedup_merge.py",
        "apps/team-orchestrator/src/orchestrator/collapse.py",
        "apps/team-orchestrator/src/orchestrator/context_builder.py",
        "apps/team-orchestrator/src/orchestrator/scheduled_triggers.py",
        "apps/team-orchestrator/src/orchestrator/agent/tools/get_attribution_data.py",
        "apps/team-orchestrator/src/orchestrator/agent/tools/get_recent_campaigns.py",
        "apps/team-orchestrator/src/orchestrator/campaign/execute.py",
        "apps/team-orchestrator/src/orchestrator/billing/attribution_close.py",
        "apps/team-orchestrator/src/orchestrator/agent/approval_resume.py",
        "apps/team-orchestrator/src/orchestrator/agent/tools/request_owner_approval.py",
        "apps/team-orchestrator/src/orchestrator/owner_inputs/writer.py",
        "apps/team-orchestrator/src/orchestrator/agent/tools/query_customer_ledger.py",
        "apps/team-orchestrator/src/orchestrator/observability/phone_tokens.py",
        "apps/team-orchestrator/src/orchestrator/integrations/dedupe.py",
        "apps/team-orchestrator/src/orchestrator/integrations/ledger.py",
        # DSR controller paths (VT-77) — service-role admin ops over many tables.
        "apps/team-orchestrator/src/orchestrator/dsr_purge.py",
        "apps/team-orchestrator/src/orchestrator/dsr_export.py",
        # VT-74 k-anonymity admission gate — THE single sanctioned cross-tenant
        # read (Pillar 8, Cowork-approved 20260603T195500Z). Queries only
        # `tenants` (not a watched hot table, so it would not trip today) via the
        # service role by design — k-anon counts tenants across the workspace and
        # cannot run inside one tenant's RLS scope. Returns tenant UUIDs + a count
        # only, never customer PII; eligible_tenant_ids are never logged/persisted.
        # Allowlisted explicitly as the documented, audited cross-tenant exception.
        "apps/team-orchestrator/src/orchestrator/privacy/k_anonymity.py",
        # VT-68 L3 construction — the 2nd sanctioned cross-tenant service-role read
        # (Pillar 8, Cowork-approved 20260604T004000Z). Aggregates campaigns +
        # customers + attributions ACROSS tenants into anonymized priors; writes
        # ONLY aggregates to l3_patterns (no tenant_id/customer id/city ever).
        # k-anon contributor gate (≥10) per cohort; no assert_tenant_scoped → no
        # Detector-1 false-trip. Same audited-exception discipline as k_anonymity.
        "apps/team-orchestrator/src/orchestrator/knowledge/l3_construction.py",
    }
)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    violations: list[str] = []
    for py in (repo_root / _SRC).rglob("*.py"):
        rel = py.relative_to(repo_root).as_posix()
        if rel in _ALLOWLIST:
            continue
        # The wrapper layer (db/) is the sanctioned access point.
        if (repo_root / _WRAPPER_DIR) in py.parents or py.parent == (repo_root / _WRAPPER_DIR):
            continue
        text = py.read_text(encoding="utf-8")
        for table, pat in _PATTERNS.items():
            m = pat.search(text)
            if m:
                violations.append(f"{rel}: direct access to '{table}' ({m.group(0)!r})")

    if violations:
        print("::error::no-direct-tenant-db-access (VT-72): NEW direct access to a "
              "tenant-scoped hot table outside the wrapper layer. Use "
              "orchestrator.db.wrappers, or (existing site) add to the allowlist "
              "ONLY with review.", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print(f"no-direct-tenant-db-access: ok ({len(_TABLES)} tables, "
          f"{len(_ALLOWLIST)} allowlisted existing sites).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
