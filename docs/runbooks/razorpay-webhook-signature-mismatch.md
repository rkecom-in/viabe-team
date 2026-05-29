# Razorpay webhook signature mismatch

## Symptom

- Razorpay webhook events not landing in DB
- Vercel function logs show 401 on `/api/webhook/razorpay`
- Razorpay dashboard → Webhooks → delivery attempts show 4xx response from our endpoint

## Detection

- Razorpay dashboard webhook delivery report
- VT-202 alert: `razorpay_webhook_4xx_rate`

## Triage

1. Confirm `RAZORPAY_WEBHOOK_SECRET` matches what's in Razorpay dashboard
2. Check the webhook handler's signature verification code path — is it using the raw request body (NOT JSON-parsed) for HMAC?
3. Confirm the webhook URL in Razorpay dashboard matches the current Vercel deploy

## Resolution

1. If secret drift: rotate `RAZORPAY_WEBHOOK_SECRET` in Razorpay dashboard + Vercel env (Fazal authorization required); restart
2. If signature verification bug: ship a fix; redeploy
3. If URL drift: update Razorpay dashboard webhook URL

## Postmortem

- Incident log
- Replay missed webhooks via Razorpay's "Resend" button in the dashboard
- File VT row if a replay-defense or sig-edge-case is needed

## Tabletop drill — last-run YYYY-MM-DD

- Outcome: NOT YET DRILLED
