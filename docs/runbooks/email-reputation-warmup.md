# Email reputation warm-up runbook (VT-113)

> Operator-facing. Resend on `send.viabe.ai` (Amazon SES backend, region ap-northeast-1 Tokyo).
> DNS verified live 2026-06-08 (SPF + MX + DKIM + DMARC). Follows the `0000-template.md` shape.

## Why warm up
A cold sending domain that suddenly sends volume looks like spam to mailbox providers → bounces +
spam-foldering that damage the domain reputation for months. Ramp volume gradually so SES/the ISPs
build a positive sending history.

## Ramp schedule (transactional only; we send no bulk)
| Week | Max emails/day | Notes |
|---|---|---|
| 1 | 50 | invite-only design partners (soft launch); watch bounce/complaint daily |
| 2 | 200 | widen if week-1 bounce < 2% AND complaint < 0.1% |
| 3 | 500 | hold if any week breached thresholds |
| 4+ | production | only after 3 clean weeks |

**Gate to advance:** the prior week's bounce rate < 5% AND complaint rate < 0.1% (the
`email_deliverability` monitor's alert thresholds). If a week breaches, HOLD at the current tier and
investigate (list hygiene, content, auth) before ramping.

## Daily check (automated + manual)
- **Automated:** the `email_deliverability` daily 10:00 IST job pulls Resend last-24h outcomes and
  alerts Fazal (Telegram ops) on bounce > 5% OR complaint > 0.1%. Fail-soft (Resend down → skip).
- **Manual (warm-up weeks):** glance at the Resend dashboard daily; confirm delivered ≫ bounced.

## If bounce/complaint spikes
1. STOP ramping; hold the current tier.
2. Check the bounce TYPE in Resend (hard = bad address → list hygiene; soft = transient).
3. Complaints > 0.1% → review content/frequency; ensure every email is wanted + has clear context.
4. Escalate per `resend-cutover.md` (DMARC tightening is the wrong lever for bounces — that's an
   auth/spoofing control, not a reputation control).

## Inbox-placement test (Fazal, pre-launch)
Send one transactional mail to 5 ESPs (Gmail, Outlook/Hotmail, Yahoo, Zoho, a corporate domain).
Confirm inbox (not spam) on each. Record results in the cutover runbook checklist.
