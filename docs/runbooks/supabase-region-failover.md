# Supabase region failover

## Symptom

- All orchestrator/team-web requests 503 with `psycopg.OperationalError` or connection-timeout
- Supabase Status page shows incident in ap-south-1 (or whichever region the project is in)

## Detection

- VT-202 alert: `supabase_unreachable`
- Customer reports of universal failure
- Supabase Status page subscription

## Triage

1. Confirm via Supabase Status page that the affected region matches the project's region (verify via VT-169 runbook output if uncertain)
2. Check whether read-replica failover is possible (Supabase Pro tier+)
3. Estimate downtime from Status page ETA

## Resolution

If short outage (< 1 hour):
1. Hold; orchestrator schedulers will retry on next tick
2. Surface to Fazal for status comms

If extended (> 1 hour):
1. Fazal authorization required for failover
2. If a replica exists in another region, switch DATABASE_URL to replica endpoint in Railway + Vercel env; restart services
3. Document downtime + recovery in incident log
4. Post-resolution: confirm no data loss; re-run any failed DBOS workflows from `workflow_status` table

## Postmortem

- Incident log
- If failover was performed: ledger entry capturing the manual region switch
- Consider filing VT row for "automated read-replica failover" if not already in scope

## Tabletop drill — last-run YYYY-MM-DD

- Outcome: NOT YET DRILLED
