# Supabase region-verify runbook (VT-169)

Verifies Supabase project data-residency against VT-18 spec (ap-south-1 dev / ap-south-2 prod) per DPDP residency.

## When to run

- After provisioning a new Supabase project (dev or prod)
- After any Supabase project migration / region change event
- During Sprint 9 polish phase as part of the privacy-notice (VT-156) accuracy gate
- When a canary log emits a non-India pooler region (the original VT-169 trigger)

## Step 0 — file new VT row if real misconfig found (LOCK 2 from review)

If the canary reports `agreement_with_brief: false` AND `warning: ""` (i.e., we have positive evidence of non-India residency, not just unknown), DO NOT bury the issue in a sprint-row edit. Allocate a new VT row first:

```bash
python3 scripts/vt_id_allocate.py
```

File `.viabe/sprint/VT-<N>.md` with status: Backlog, priority Critical, capturing the migration task. Then proceed with the steps below.

## Step 1 — Run the canary

```bash
cd apps/team-orchestrator
(
  set -a
  source ../../.viabe/secrets/supabase-dev.env
  set +a
  ./.venv/bin/python canaries/vt169_db_region_residency.py
)
```

Output is a JSON block with:
- `pooler_hostname`, `pooler_region`
- `resolved_ip`, `aws_region_for_ip`
- `db_server_addr`, `db_advertised_region`
- `supabase_api_region` (if `SUPABASE_MGMT_TOKEN` env set)
- `agreement_with_brief` (true/false)
- `warning` (set to `UNKNOWN_NEEDS_DASHBOARD_VERIFY` when the canary can't determine residency from inside the DB)

## Step 2 — Interpret the result

| Canary result | Interpretation | Action |
|---|---|---|
| `agreement_with_brief: true`, `warning: ""` | Residency confirmed | Update VT-18.md + decisions-ledger.md; flip VT-169 → Done |
| `agreement_with_brief: true`, `warning: UNKNOWN_NEEDS_DASHBOARD_VERIFY` | Pooler-only data; cannot confirm from inside | Proceed to Step 3 (dashboard cross-check) |
| `agreement_with_brief: false`, `aws_region_for_ip` in non-India region | Real misconfig (Interpretation 1) | Step 0 + Step 4 (Type-3 escalation) |
| Pooler region ≠ India but `db_advertised_region` in India | Pooler topology only (Interpretation 3) | Document in VT-18.md as Interpretation 3; flip VT-169 → Done |

## Step 3 — Dashboard cross-check (Fazal-side)

CC has no Supabase dashboard credentials (orchestrator-process-only per CL-390 cluster). Fazal action required:

1. Vercel → Supabase dashboard → viabe-team-dev project → Settings → General → Region
2. Note the stated region
3. Relay back to Cowork

For automation: drop a Supabase management API token into env as `SUPABASE_MGMT_TOKEN`. Then the canary auto-cross-checks via `/v1/projects/{ref}` (Supabase REST API). Token rotation procedure: regenerate in Supabase dashboard → Account → Access Tokens.

## Step 4 — Decision matrix (Fazal Type-3 if real misconfig)

If the canary + dashboard converge on a real misconfiguration:

| Decision | Path |
|---|---|
| **Migrate to ap-south-1/2** | Filed VT row from Step 0; provision new project; migrate data (downtime); update DATABASE_URL in Railway env; rerun canary; flip VT-169 |
| **Accept non-India + document DPDP impact** | Write `docs/team/dpdp-region-impact-analysis.md` signed by Fazal; update privacy notice (VT-156) accordingly; update decisions-ledger.md as Standing |
| **Dev/prod drift** (dev stays misconfigured, prod fixed) | Document the drift in decisions-ledger.md; ensure dev tenants are flagged as non-production |

## Step 5 — Update VT-18 + decisions-ledger

Update `.viabe/sprint/VT-18.md` Notes section with the verified region. Update `docs/clau/decisions-ledger.md` as a Standing decision capturing the region for dev + prod.

## Step 6 — Flip VT-169 → Done

```bash
# .viabe/sprint/VT-169.md frontmatter
status: Done
last_updated: <today>
done_note: "<one-liner summarising verified region per canary + dashboard>"
```

## Cross-refs

- Discovered by: VT-102 canary commit `957887e` (PREFLIGHT host echo)
- Spec source: `.viabe/sprint/VT-18.md` (parent: VT-Foundation)
- Gates: VT-156 (privacy notice), VT-115 (final legal/security review)
- Related Standing decisions: CL-385 / CL-390 (privacy cluster); CL-416 (lifetime retention — residency-bound)
