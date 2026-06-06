# Launch-mode transitions (VT-97)

The public surface (`/team`, `/team/signup`) renders by `NEXT_PUBLIC_TEAM_LAUNCH_MODE`:
`waitlist | live | maintenance` (default `waitlist`). Build-time (Vercel env var, project
root-dir `apps/team-web`); a redeploy flips it. One toggle picks the whole rendering tree
(Pillar 8) — `lib/launch-mode.ts`.

## Modes
- **waitlist** — hero "Join the waitlist" + the 2-field capture form (email + WhatsApp + one
  mandatory consent). Pricing / day-39 / FAQ stay (informational). `/team/signup` redirects to
  `/team`.
- **live** — the full signup flow (VT-96 OTP-before-create).
- **maintenance** — a single "back soon" notice, no form. For launch hot-fixes.

## Flipping the mode
1. Set `NEXT_PUBLIC_TEAM_LAUNCH_MODE=<mode>` in Vercel (project = `apps/team-web`).
2. Redeploy — the value is build-time inlined (a runtime client read would hydration-mismatch).
3. Downtime per transition: <60s (Vercel deploy).

## CL-422 — collecting REAL waitlist PII (hard gate)
The `waitlist` MODE renders the form everywhere, but the form only **collects** when
`ENABLE_WAITLIST_CAPTURE=true` (the `/api/team/waitlist` proxy 404s otherwise). Flipping that to
collect real entries is gated on **VT-231 (Mumbai prod) + Fazal** — exactly like
`ENABLE_PUBLIC_SIGNUP`. **Never set `ENABLE_WAITLIST_CAPTURE` on dev (Seoul).**

## waitlist → live (launch day)
1. Sweep `waitlist_signups`: send the launch-announcement WhatsApp template to all rows, set
   `notified_at`.
2. `purge_notified_waitlist()` — purge the notified rows (purpose fulfilled; see
   `docs/policy/waitlist-data.md`).
3. Flip `NEXT_PUBLIC_TEAM_LAUNCH_MODE=live` + `ENABLE_PUBLIC_SIGNUP=true` + redeploy.

## Erasure / retention
Waitlist erasure is its OWN path (the table is pre-tenant, not in the tenant DSR) — see
`docs/policy/waitlist-data.md`: the `DELETE /api/waitlist` ops endpoint, `purge_notified_waitlist()`,
and the `purge_stale_unnotified(months=6)` retention bound.
