# Operator JWT secret compromise

## Symptom

- `OPERATOR_JWT_SECRET` leaked (committed to git, exposed in a screenshot, posted in a chat, found in logs)
- Suspicious activity in `pipeline_steps` from unexpected operator IPs

## Detection

- Git history scan (`git log -p | grep -i operator_jwt_secret`)
- gitleaks CI gate (should have caught at PR time; if not, immediate post-merge audit)
- Manual operator review

## Triage

1. Confirm the leak is real (not a false positive from a test fixture)
2. Identify the leaked value's exposure window (commit time → discovery time)
3. Estimate harm: any operator-claim JWTs issued from this secret are now forgeable

## Resolution (high urgency, Fazal authorization required)

1. **Immediately** rotate `OPERATOR_JWT_SECRET`:
   - Generate new: `openssl rand -hex 32`
   - Update Railway env (orchestrator)
   - Update Vercel env (team-web, where used)
   - Restart both services
2. All existing operator JWTs (Fazal sessions) become invalid; Fazal must re-login
3. If any non-Fazal session was active, force-invalidate via Supabase Auth admin API
4. Scrub the leaked secret from git history (BFG repo-cleaner) — Fazal authorization for force-push
5. Audit `admin_audit_log` + `pipeline_steps` for any unexpected operator-claim activity during the exposure window

## Postmortem

- Critical incident log
- Ledger entry capturing the rotation event
- Confirm gitleaks gate covers the pattern; if not, extend
- Customer comms IF customer data was viewed via the compromised JWT

## Tabletop drill — last-run YYYY-MM-DD

- Outcome: NOT YET DRILLED — high priority for Sprint 9 polish
