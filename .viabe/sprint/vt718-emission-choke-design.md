# VT-718 — S2 single emission choke: design note

Status: BUILT (CC, 2026-07-29) — implementation landed as designed (deltas in §8); shadow-mode
dev proof next; Clau audit-after.
Authorization: CL-2026-07-28-single-voice-manager + Fazal priority build grant (2026-07-29 0900Z relay).

## 1. The core decision: the choke lives IN the transport, not above it

Recon found **38 files** calling the send primitives (`send_freeform_message` /
`send_interactive_message` / `send_template_message` / `send_owner_template`) — not the ~7
"speaking surfaces" of the original estimate. Two consequences:

1. A "migrate every caller to a new `emit_owner_*` wrapper" plan is a 38-file churn PR with
   regression risk on every surface, and it stays bypassable (a 39th caller imports the raw
   primitive and review misses it).
2. `utils/twilio_send.py` is **already the single physical funnel**: it is the only module that
   constructs the Twilio client, all three primitives pass through it, and it already
   discriminates owner vs customer per-send (`is_customer_send` / `is_customer_session` — the
   VT-460 flags). It already hosts two structural chokes we mirror exactly:
   - `_assert_gated_if_customer` + `customer_send_context()` (VT-460) — fail-closed customer boundary;
   - the OWNER_TEMPLATE_WHITELIST shadow-first enforcement (VT-683 P4).

**Therefore: the S2 choke is `_owner_emission_guard(...)` invoked inside all three primitives on
the owner branch (`is_customer_* == False`), before `messages.create`.** Every existing caller and
every FUTURE caller is choked automatically — no migration, no bypassable wrapper. The CI gate
(§5) makes the funnel itself unescapable.

## 2. What the guard does (deterministic, zero added latency)

Mode env `TEAM_OWNER_EMISSION_CHOKE` (feature_flags `_on` house pattern):
`off` (byte-identical today) → `shadow` (log-only "would suppress") → `enforce`. Dev: shadow →
×3 proof → enforce. Prod: Fazal flip only.

### 2a. Dedup / double-reply block (the run-6(b) fix)
Suppression rule — an outbound owner send is a DUPLICATE iff:
- normalized body (casefold, NFC, whitespace/punct collapse, 200-char head — the VT-716
  typed-twice normalizer, reused) exactly matches a prior outbound to the same recipient
  within WINDOW (default 180s), **and**
- **no inbound owner turn arrived between the two sends.** This condition is what makes the
  guard safe: a legitimate verbatim re-ask (invalid answer → re-present) always follows an
  inbound; the double-reply disease is the Manager speaking twice with no owner turn between.

Two layers:
- **L1 in-process** per-recipient-token ring buffer (last ~8 outbound bodies + ts + a
  monotonic inbound marker) — catches the 4s burst class with zero DB cost, works even
  tenant-blind.
- **L2 conversation_log check** (only when `tenant_id` known, only if L1 passes and body matches
  the window): SELECT recent turns, apply the same rule cross-restart. Fail-open on DB error
  (a send must never die on a memory read), log WARN.

Suppressed send returns cleanly (freeform/interactive → sentinel sid `"choke-suppressed"`;
template → SendResult success=True, error_code `emission_suppressed`) — NEVER raises, because
every surface has a "fall back to freeform on failure" ladder that would resend the very text
we suppressed. Suppressions log at WARNING with surface + recipient token for the judge.

### 2b. Inbound marker
The guard needs "did the owner speak between my two sends". `conversation_log.record_turn` for
role='owner' already runs on every inbound (runner) — L2 reads it. L1 gets a cheap
`note_owner_inbound(recipient_token)` hook called from the runner's inbound leg (one line).

### 2c. Contradiction / continuity slot (S3/S4 hook — built as a seam now, no-op today)
`_owner_emission_guard` takes a pluggable check list. VT-719 plugs the asserted-facts ledger
consult here (owned-change framing check); S4 plugs continuity. Nothing LLM runs in-line in S2 —
latency budget stays untouched (Fazal's ≥10s complaint).

## 3. Tenant plumbing (the one real caller change)
The turn-brain reply path (`journey._send_turn` → `send_freeform_message(body, recipient)`) is
tenant-blind at the transport (it records via `_append_recent_turns` to avoid double-log).
Fix: new transport kwarg `record_turn: bool = True`; journey passes
`tenant_id=..., record_turn=False`. L2 dedup then covers the highest-risk surface (turn-brain)
without double-logging. Same for the two other tenant-blind sites recon confirms.
`_record_owner_conversation_turn` keys off `record_turn and tenant_id`.

## 4. Effect gates: UNCHANGED (§0.1.1)
The choke governs the VOICE only. `_assert_gated_if_customer` (customer), consent/opt-out/
approval/onboarded/money gates: untouched, still upstream. **Explicit new test:** a
Manager-plan-approved action whose execution reaches a customer send / money / consent effect
still stops at the deterministic gate — plan-approval must not carry an effect past a gate
(Tier-1 class if it does). Plus: the choke can only SUPPRESS, never create/approve a send.

## 5. The CI no-bypass gate — `gate-owner-emission-choke`
`scripts/check_owner_emission_choke.py` (pattern: check_no_raw_railway_variables.py; wired into
ci.yml + `ci-success` needs + pre-push). Hard-fails when:
1. Twilio client construction (`from twilio` / `Client(` / `api.twilio.com` / `messages.create`)
   appears outside `utils/twilio_send.py` (+ `utils/dev_send_guard.py`, tests) — the funnel is
   the choke, so escaping the funnel is the ONLY bypass; this makes it a CI failure, not a
   review catch.
2. The three primitives' owner branches drift: the gate asserts `_owner_emission_guard(` is
   called in each primitive (a text-anchor self-check, so a refactor can't silently drop the
   guard).

## 6. Rollout + proof
1. Land guard (off) + unit tests (suppression rule truth table incl. inbound-between, fail-open,
   sentinel returns, §0.1.1 effect-gate test) + CI gate.
2. Dev `shadow`: drive the completion-boundary scenario + full-pack ×3; judge reads
   "would-suppress" WARNs — expected hits: run-6(b) double-reply; zero hits on legit re-asks.
3. Dev `enforce`: full-pack ×3 green → checkpoint push.
4. Prod: Fazal flip at his word.

## 7. Explicitly NOT in S2
- No caller migration (S4 route unification decides what feeds the composer).
- No LLM contradiction check (S3 ledger provides the deterministic substrate first).
- Run-6(a) consent-decline misfire is a separate tactical fix (pre_filter_gate
  `classify_consent_intent` floor: bare-negation token inside a longer benign sentence —
  "No changes, looks good" — must not read as decline; ships alongside, own commit).

## 8. As-built deltas (2026-07-29)
- **TEMPLATE sends are NOT deduped** (documented in the transport block): the whitelisted owner
  templates carry effectful asks (approvals) — two DISTINCT asks via the same template with no
  owner reply between are legitimate, and suppressing an approval ask is the worst failure class
  for this guard. The double-reply disease lives in the freeform/interactive session voice.
- **Failed sends never enter the dedup ring** (`_l1_dupe` is check-only; `_note_owner_emission`
  records post-success only) — otherwise a failed interactive attempt would poison the ring and
  ENFORCE would suppress the freeform FALLBACK of the same text, breaking every fallback ladder.
- **Fixed en passant:** the paced-flow suggestion-button path double-recorded to conversation_log
  (transport + `_record_flow_turn`); the `record_turn` plumbing makes every journey path
  single-recorder.
- **Run-6(a) shipped with this row:** `_CONSENT_DECLINE_BARE_NEG` — bare negations decline only
  in ≤3-token replies; explicit deferrals/phrases and the strictly-deterministic AFFIRM side
  unchanged.
- `auth/twilio_verify.py` (login OTP, Verify API, dev-guard wrapped) is the one sanctioned
  non-transport Twilio egress — allowlisted in the CI gate with rationale.
