/**
 * VT-226 — fire-and-forget webhook_metrics emit from team-web edge.
 *
 * POSTs to the orchestrator admin endpoint. Orchestrator enqueues a
 * DBOS workflow that handles the actual INSERT with retry semantics.
 * Caller does NOT await; latency budget stays inside the Twilio
 * webhook's response window.
 */

import { redactForLog } from '@/lib/log-redact'

export interface WebhookMetric {
  source: 'twilio' | 'razorpay' | 'shopify' | 'google_drive'
  event: 'sig_pass' | 'sig_fail' | 'replay_rejected' | 'rate_limit_rejected'
  message_sid?: string | null
  source_ip: string
  response_status: number
}

export function emitWebhookMetric(metric: WebhookMetric): void {
  // Fire and forget — never await; never block the route response.
  const orchUrl = process.env.TEAM_ORCHESTRATOR_URL ?? ''
  const adminToken = process.env.TEAM_ADMIN_API_TOKEN ?? ''
  if (!orchUrl || !adminToken) {
    // Config gap — log once, don't block. Operator surfaces this via
    // admin/health endpoint when the metric volume is unexpectedly zero.
    return
  }

  // Wrap in IIFE so any thrown error inside the promise chain stays
  // captured. Vercel may kill the function before the POST completes;
  // that's an acceptable trade for not blocking the Twilio response.
  void (async () => {
    try {
      await fetch(`${orchUrl}/api/orchestrator/admin/webhook_metrics/record`, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'X-Team-Admin-Token': adminToken,
        },
        body: JSON.stringify(metric),
        signal: AbortSignal.timeout(2000),
      })
    } catch (err) {
      console.error(
        redactForLog(
          JSON.stringify({
            event: 'webhook_metric_emit_failed',
            reason: err instanceof Error ? err.message : 'unknown',
            metric_source: metric.source,
            metric_event: metric.event,
          }),
        ),
      )
    }
  })()
}
