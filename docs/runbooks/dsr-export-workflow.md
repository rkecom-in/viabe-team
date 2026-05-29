# DPDP data-subject-request export

## Symptom

- Owner requests their data via the documented DSR channel (privacy@viabe.ai or similar)
- Per CL-330 / CL-416: lifetime retention; DSR-purge is the sole deletion path; DSR-export is the read variant

## Detection

- Operator-driven (incoming email or formal DPDP request)

## Triage

1. Confirm the requester's identity (email match, tenant ownership claim, contractual proof)
2. Identify tenant_id from `tenants` table by business_name / whatsapp_number / contact email
3. Note DSR request type: export OR purge (or both)

## Resolution — Export

1. Run DSR-export workflow via admin endpoint (TBD: VT-N + dedicated `/admin/dsr/export?tenant_id=...`)
2. The workflow gathers from: `tenants`, `tenant_oauth_tokens` (shape only, no raw token), `pipeline_runs`, `pipeline_steps`, `owner_inputs`, `phone_token_resolutions`, `customers`, L0 fragments (if consent flag was on)
3. Output as a single ZIP (JSON per table) emailed to the verified address
4. Audit log via admin_audit_log

## Resolution — Purge

1. Same identity confirmation
2. Run DSR-purge workflow via admin endpoint (TBD: VT-N)
3. Deletes from same tables in reverse-dependency order (children first → parent `tenants` row last)
4. Audit log

## Postmortem

- Incident log
- File VT row if any new substrate is added that needs DSR coverage (every new table touching tenant data should land DSR-export wiring in the same PR)

## Tabletop drill — last-run YYYY-MM-DD

- Outcome: NOT YET DRILLED — DSR-export workflow is not yet built (see VT-185 v2.0 + future row)
