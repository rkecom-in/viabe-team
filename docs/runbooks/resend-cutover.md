# Resend cutover + DMARC escalation runbook (VT-113)

> Operator-facing. The DNS layer is LIVE + verified (2026-06-08). This runbook = verification
> commands, the DMARC tightening path, rollback, and the deliverability-monitor endpoint caveat.

## Live DNS records (verified 2026-06-08 against authoritative DNS)
| Record | Value |
|---|---|
| `send.viabe.ai` TXT (SPF) | `v=spf1 include:amazonses.com ~all` |
| `send.viabe.ai` MX | `10 feedback-smtp.ap-northeast-1.amazonses.com` (region ap-northeast-1, Tokyo) |
| `resend._domainkey.viabe.ai` TXT | DKIM public key |
| `_dmarc.viabe.ai` TXT | `v=DMARC1; p=none; rua=mailto:info@rkecom.in; ri=86400; sp=none` |
| `viabe.ai._report._dmarc.rkecom.in` TXT | `v=DMARC1` (external-destination authorization for rua) |

## Verify (dig)
```
dig +short TXT send.viabe.ai
dig +short MX  send.viabe.ai
dig +short TXT resend._domainkey.viabe.ai
dig +short TXT _dmarc.viabe.ai
dig +short TXT viabe.ai._report._dmarc.rkecom.in   # external-dest auth (else rua to rkecom.in is ignored)
```
All five must resolve. DKIM/SPF must align for DMARC to pass.

## DMARC escalation: p=none → quarantine → reject
Phase-1 is **p=none** (monitor only — no mail is blocked). Reports land at info@rkecom.in (manual
weekly review; NO Postmark/parser vendor pre-launch). Tighten ONLY when reports show clean alignment:

| Stage | Criteria to advance |
|---|---|
| `p=none` (now) | baseline; collect ≥ 2 weeks of rua reports |
| `p=quarantine` | ≥ 2 weeks of reports showing ~100% of OUR mail SPF+DKIM-aligned, no legit source failing |
| `p=reject` | ≥ 2 more weeks clean at quarantine; no legitimate mail quarantined |

Never jump straight to reject — a misaligned legit sender (a forgotten SES identity, a third-party
sender) would have its mail hard-bounced.

## Rollback
- Deliverability tanks after a DMARC tighten → revert `_dmarc.viabe.ai` `p=` to the prior stage
  (DNS TXT edit; propagates in ~`ri`/TTL). DMARC policy is a DNS change, not a deploy.
- Resend/SES outage → mail just fails to send (fail-soft); no rollback needed; monitor alerts.

## Deliverability monitor — ENDPOINT CAVEAT (VT-113, the #420 vendor-shape lesson)
`alerts/email_deliverability.py` polls Resend for last-24h outcomes (`GET /emails`, counting
`last_event`). **Resend delivers bounce/complaint primarily via WEBHOOKS** (email.bounced /
email.complained); a single aggregate poll-stats endpoint is NOT clearly documented. The request
shape is pinned by a unit test, but the LIVE call shape is UNCONFIRMED until the gated canary
(`test_real_resend_stats_call`, run with `RESEND_LIVE_CANARY=1` + `RESEND_API_KEY` from a
network-unblocked host) passes. If it fails, switch to: (a) the real list-endpoint field, or
(b) a Resend-webhook ingress (email.bounced/complained → a counts table). Until then the monitor is
fail-soft: a wrong endpoint → vendor-down → skip (no false alerts, no crash) — degraded, not dangerous.

## Inbox-placement checklist (Fazal, pre-launch manual 5-ESP test)
- [ ] Gmail — inbox (not spam/promotions)
- [ ] Outlook / Hotmail — inbox
- [ ] Yahoo — inbox
- [ ] Zoho — inbox
- [ ] A corporate domain — inbox
Record date + results here before flipping launch mode.
