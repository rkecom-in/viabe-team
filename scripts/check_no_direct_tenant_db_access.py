#!/usr/bin/env python3
"""VT-72 / VT-306 / VT-324 — `no-direct-tenant-db-access` ENFORCED gate.

Forbids direct SQL access to the tenant-scoped hot tables outside the wrapper
layer. VT-306 migrated every per-tenant call site onto orchestrator.db.wrappers
and shrank the allowlist to the 11 STRUCTURAL residuals (BYPASSRLS / operator-role
/ cross-tenant sweeps), each with a documented reason inline. VT-324 wires this as
a BLOCKING pre-push check (post-VT-245, CI status checks don't gate merges, so the
pre-push hook is the real safety gate) — no longer report-only.

Any non-allowlisted file touching these tables — a NEW direct-SQL site, or a
migrated site that regresses — fails the gate.

Exit 0 = clean (only the 11 residual files / the wrapper layer touch the tables).
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
    "platform_listings",  # VT-325: new tenant hot table — wrapper-only from day one
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
        # VT-306 RESIDUAL: only sites that CANNOT use the per-tenant app_role
        # wrapper (BYPASSRLS-by-design / operator-role / cross-tenant), each with a
        # documented reason. The ~15 per-tenant Group-A sites were MIGRATED onto
        # orchestrator.db.wrappers and removed from this list. NEVER re-add a
        # "deferred, not-yet-migrated" site here — the residual stays structural.
        #
        # DSR controller paths (VT-77) — service-role admin ops over many tables;
        # explicit WHERE tenant_id. Tenant-wide deletion/export cannot run inside a
        # single tenant's RLS scope.
        "apps/team-orchestrator/src/orchestrator/dsr_purge.py",
        "apps/team-orchestrator/src/orchestrator/dsr_export.py",
        # VT-28/176/47 scheduled-sweep eligibility scans — the nightly bodies scan
        # ALL eligible campaigns (attribution-close) + ALL timed-out
        # pending_approvals ACROSS tenants under BYPASSRLS, by design: a sweep
        # cannot run inside one tenant's RLS scope. The per-row work then routes
        # through tenant-scoped paths (close_attribution / mark_approval_resolved,
        # tenant-predicated). Cross-tenant scan = residual, like the other sweeps.
        "apps/team-orchestrator/src/orchestrator/scheduled_triggers.py",
        # VT-176 attribution close — by-PK UPDATE of `campaigns` under BYPASSRLS in
        # the scheduled cross-tenant sweep; the id is sourced from the INTERNAL
        # sweep query, never client input (no IDOR surface) (Cowork 20260605T002000Z).
        "apps/team-orchestrator/src/orchestrator/billing/attribution_close.py",
        # phone_token_resolutions is OPERATOR-ROLE substrate (CL-82): mig 027 grants
        # SELECT to app_operator_role; mig 007 has NO app_role grant, so a
        # tenant_connection (SET ROLE app_role) read is permission-denied by
        # construction. All access is service-role + explicit WHERE tenant_id, by
        # necessity (Cowork 20260605T002800Z). phone_tokens + dedupe ADDITIONALLY
        # bootstrap tenant context (resolve token -> FIND the tenant; no GUC can be
        # set before the lookup). query_customer_ledger + ledger are in-context
        # reads but still on the operator-role table.
        "apps/team-orchestrator/src/orchestrator/observability/phone_tokens.py",
        "apps/team-orchestrator/src/orchestrator/integrations/dedupe.py",
        "apps/team-orchestrator/src/orchestrator/agent/tools/query_customer_ledger.py",
        "apps/team-orchestrator/src/orchestrator/integrations/ledger.py",
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
        # VT-76 reconstitution sweep — the 3rd sanctioned cross-tenant service-role
        # read (Cowork-approved 20260604T033000Z). The daily sweep's eligibility +
        # 8-day-SLA scans find opted-out customers ACROSS the workspace, which a
        # per-tenant RLS wrapper physically cannot do (it would need to enumerate
        # tenants first). Both scans project ids + opt_out_at ONLY — never
        # display_name/phone/email (CL-390). The actual anonymization write goes
        # through `tenant_connection` (RLS), not direct access. Same documented,
        # audited cross-tenant-exception discipline as k_anonymity / l3_construction.
        "apps/team-orchestrator/src/orchestrator/privacy/reconstitution.py",
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
