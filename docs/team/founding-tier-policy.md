# Founding-tier policy (VT-94)

The first **100** paid customers get **founding** pricing (₹2,499/mo), locked for the
lifetime of their subscription.

## No-release (no-reopen) policy
Once a founding slot is claimed, it stays counted **even if the tenant later churns**
(cancels / refunds / DSR-deletes). The counter NEVER decrements.

`founding_tier_claims.released_at` is stamped on churn for **audit only** — it does NOT
return the slot to the pool. Rationale: re-opening founding slots later would dilute the
"first 100" promise made to the original founding tenants. The founding tier closes
permanently once 100 are claimed.

## Atomicity + integrity
- The claim is a single atomic `UPDATE founding_tier_counter SET claimed_count =
  claimed_count + 1 WHERE id = 1 AND claimed_count < cap RETURNING` on a sentinel row —
  race-safe by Postgres row-level locking. Exactly 100 ever succeed (never 101).
- The claim runs INSIDE the signup transaction, so a rolled-back signup never leaks a
  permanent slot.
- `cap = 100` is a Type-3 commitment; the literal 100 lives in a CHECK constraint —
  raising it is a governance decision, not a config tweak.
- `founding_tier_claims.tenant_id` is UNIQUE — one slot per tenant; a re-claim never
  double-counts.

## Pricing lock
A founding tenant's Razorpay subscription is created at `FOUNDING_RZP_PLAN_ID` (₹2,499);
future plan-price changes do NOT affect existing founding subscriptions (Pillar 7,
Type-3 commitment).

## Public counter
`GET /api/team/founding-status` exposes `{remaining, cap, public_count, all_claimed}` —
no auth, cached ~60s — for the landing-site widget (VT-99). The count is approximate for
display; absolute correctness is enforced only at claim time (the atomic UPDATE).
