# Apify actor failure

## Symptom

- Apify actor runs returning `FAILED` status
- VT-202 alert: `apify_actor_failures`
- Ingestion worker logs (when wired) show no successful pulls

## Detection

- Apify console → Actor runs → status filter
- VT-202 alert via Telegram

## Triage

1. Check Apify actor run logs for the latest failure
2. Confirm Apify account has not exceeded compute units (free tier limits)
3. Confirm the target site (Instagram, Zomato, etc.) hasn't changed its anti-scraping posture

## Resolution

1. If quota: top up Apify credits (Fazal authorization)
2. If actor regression: file VT row to patch the actor script
3. If anti-scraping: surface to Fazal — may need to switch to a different actor or accept degraded ingestion

## Postmortem

- Incident log
- File VT row for actor maintenance if recurring

## Tabletop drill — last-run YYYY-MM-DD

- Outcome: NOT YET DRILLED — apps/team-ingestion-worker is a SystemExit stub at time of writing
