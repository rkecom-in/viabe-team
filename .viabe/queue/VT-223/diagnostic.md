# VT-223 sticky-deploy diagnostic — 2026-05-28 incident

## Symptom

Fazal reported a Vercel production deploy stuck in-progress for ~2 hours on 2026-05-28. Manual cancel + redeploy from the Vercel dashboard unstuck it. Approximate timestamp: ~17:50 UTC (matches the VT-220 squash-merge window).

## Logs

Not retained on the CC side. Vercel UI retains build logs ~7 days but CC has no Vercel API token for programmatic capture. Vercel-side build logs may still be available in the dashboard if pulled within the retention window.

## Likely causes (ranked by probability given timing + repo state)

### 1. Webhook race during VT-219 force-push (HIGH)

VT-219's commit chain was:
1. Initial commit `1a77a32`
2. Title gate failed; rebased onto main → `eb9a133`
3. Author-fix amend + force-push

The Vercel webhook listener fires on `push` events. A force-push that supersedes a previously-tracked commit can leave Vercel with a deploy reference pointing at the orphaned SHA. If Vercel's internal job-queue processes the orphan after the replacement deploy has already started, the orphan job can hang waiting on a no-longer-existing ref. This is consistent with the timing (force-push around the VT-219/VT-220 window) + the fact that manual cancel + redeploy from the live HEAD unstuck it.

**Mitigation:** avoid force-pushes during active Vercel deploys when possible. If unavoidable, monitor the Vercel dashboard for stuck deploys + cancel.

### 2. Build timeout (LOW)

Vercel default: 45 min. team-web's `pnpm build` typically completes in ~2 min. A 2h stuck state implies the build was wedged, not running. Pre-deploy `pnpm install --frozen-lockfile` could in theory hang on a registry stall, but two hours is well past Vercel's normal timeout cutoff.

### 3. ignoreCommand exit-code ambiguity (LOW)

`vercel.json:6` runs `git diff --quiet HEAD^ HEAD ./`. If the subshell hangs (e.g., FS lock contention on the build runner) without exiting, Vercel may not have a clean kill signal. Mitigation: add a `timeout 10s` wrapper around the ignore command. Not shipping that change in this PR — deferring until we have evidence the ignore command itself is the culprit.

## Monitoring approach for next occurrence

Enable Vercel **Deployment Status Webhooks** in project settings → Webhooks tab. Subscribe to `deployment.error` + `deployment.canceled` + `deployment.created` events. Forward via the orchestrator's existing webhook substrate into the ops alert channel (VT-202 alert routing). Next stuck deploy will have:

1. Wall-clock timestamp of state transitions
2. Full Vercel build log URL embedded in the webhook payload
3. SHA + branch context for the deploy in question

Filed as future-work in `docs/clau/dev-env-runbook.md` "Preview Deploys" section.

## Acceptance

This diagnostic file satisfies VT-223 acceptance #2: "Diagnostic file for the sticky deploy in `.viabe/queue/VT-223/diagnostic.md` with root cause OR 'couldn't reproduce; no logs retained'". Root cause is "no retained logs; highest-probability hypothesis is the VT-219 force-push webhook race".

## Owner

CC + Fazal (Vercel-dashboard work).

## Status

Hypothesis-level analysis complete; awaiting next occurrence (with webhook-instrumentation enabled per future-work item) to confirm or refute the force-push race theory.
