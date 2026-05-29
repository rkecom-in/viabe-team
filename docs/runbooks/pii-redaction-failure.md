# PII redaction failure

## Symptom

- Logs / alert payloads / audit entries contain raw PII (E.164 phones, customer names, message bodies)
- Per CL-390 / CL-104 / VT-202: PII scrub is load-bearing on every external-facing surface

## Detection

- Operator notices raw phone in an alert email/Telegram
- Code review surfaces a new log line that emits raw `customer.phone_e164`
- Canary regression (VT-202 A7 PII scrub assertion fails)

## Triage

1. Identify the surface: email, Telegram, log line, admin audit, etc.
2. Check whether the new code path bypassed the existing `_scrub` helper (VT-202 alerts/pii_scrub.py)
3. Estimate exposure (how many messages already sent with raw PII)

## Resolution

1. Stop the failing surface immediately (disable the alert sender / log line; deploy fix)
2. Recall sent messages if possible (Telegram edit_message; Resend has no recall)
3. Ship a fix routing the data through the `_scrub` helper before send
4. Add a regression assertion to the relevant canary (VT-202 A7 pattern)

## Postmortem

- Incident log + DPDP impact note
- Ledger entry if the redaction policy needs tightening (e.g., add a new pattern to the regex)
- If extended exposure: Fazal-led customer comms

## Tabletop drill — last-run YYYY-MM-DD

- Outcome: NOT YET DRILLED
