'use client'

/**
 * VT-267 PR-C — owner onboarding wizard (client). Renders inside the WhatsApp in-app browser.
 *
 * Review-&-Confirm: per-field edit states over the draft business_profile (save → MERGE via the
 * orchestrator). Connect steps: a TAPPABLE link (target=_blank, rel=noopener) hands the OAuth off
 * to the SYSTEM browser — NEVER a JS window.open popup (blocked in the WhatsApp WebView). After the
 * owner returns, "I've connected" re-checks the true status server-side to resume (no client trust).
 */

import { useState, useTransition } from 'react'

import {
  checkConnectionAction,
  saveProfileAction,
  startConnectAction,
} from '@/app/(app)/team/onboard/wizard/actions'
import type { WizardConnector } from '@/lib/onboard/connect'
import {
  WIZARD_EDITABLE_FIELDS,
  type EditableField,
  type ProfileDraft,
} from '@/lib/onboard/profile'

const FIELD_LABEL: Record<EditableField, string> = {
  business_name: 'Business name',
  business_type: 'Business type',
  preferred_language: 'Preferred language',
  owner_curated_context: 'Anything else we should know',
}

const CONNECTOR_LABEL: Record<WizardConnector, string> = {
  whatsapp: 'WhatsApp',
  google_sheet: 'Google Sheets',
  shopify: 'Shopify',
}

export function OnboardingWizard({
  draft,
  initialSheets,
  initialWhatsapp,
  initialShopify,
}: {
  draft: ProfileDraft
  initialSheets: boolean
  initialWhatsapp: boolean
  initialShopify: boolean
}) {
  const [pending, startTransition] = useTransition()
  const [fields, setFields] = useState<Record<EditableField, string>>({
    business_name: draft.business_name,
    business_type: draft.business_type,
    preferred_language: draft.preferred_language,
    owner_curated_context: draft.owner_curated_context,
  })
  const [editing, setEditing] = useState<EditableField | null>(null)
  const [saveMsg, setSaveMsg] = useState<string>('')

  const [connected, setConnected] = useState<Record<WizardConnector, boolean>>({
    google_sheet: initialSheets,
    whatsapp: initialWhatsapp,
    shopify: initialShopify,
  })
  const [authUrls, setAuthUrls] = useState<Partial<Record<WizardConnector, string>>>({})
  const [connectMsg, setConnectMsg] = useState<Partial<Record<WizardConnector, string>>>({})
  // Sweep #16 (VT-453): per-connector recovery flag. Set when a re-check returns a connected-but-
  // not-fully-ingesting state (Shopify: token stored, order webhooks never registered). Surfaces a
  // re-register affordance so the merchant isn't silently connected-but-uningested.
  const [needsReregister, setNeedsReregister] = useState<Partial<Record<WizardConnector, boolean>>>({})
  // VT-422 GAP-3: the owner's *.myshopify.com domain — the one UI addition Shopify needs
  // (sheets/whatsapp don't). Passed to startConnect → /setup; the tenant is session-resolved
  // server-side, never client-trusted.
  const [shopDomain, setShopDomain] = useState<string>('')

  function saveField(field: EditableField) {
    startTransition(async () => {
      const res = await saveProfileAction({ [field]: fields[field] })
      setSaveMsg(res.ok ? `Saved ${FIELD_LABEL[field]}` : `Couldn't save: ${res.reason}`)
      if (res.ok) setEditing(null)
    })
  }

  function beginConnect(connector: WizardConnector) {
    startTransition(async () => {
      // VT-422 GAP-3: shopify needs the owner-typed shop domain; others ignore it.
      const res = await startConnectAction(
        connector,
        connector === 'shopify' ? shopDomain.trim() : undefined,
      )
      if (res.ok && res.authUrl) {
        setAuthUrls((u) => ({ ...u, [connector]: res.authUrl! }))
        setConnectMsg((m) => ({ ...m, [connector]: 'Open the link in your browser, then tap “I’ve connected”.' }))
      } else {
        setConnectMsg((m) => ({ ...m, [connector]: `Couldn't start: ${res.reason}` }))
      }
    })
  }

  function recheck(connector: WizardConnector) {
    startTransition(async () => {
      const res = await checkConnectionAction(connector)
      setConnected((c) => ({ ...c, [connector]: res.connected }))
      // Sweep #16: a connected-but-webhooks-unregistered Shopify install reads back as NOT connected
      // with actionRequired='reregister_webhooks'. Surface a re-register affordance + an honest message
      // so the merchant isn't silently connected-but-uningested (go-forward orders never deliver).
      const reregister = res.actionRequired === 'reregister_webhooks'
      setNeedsReregister((r) => ({ ...r, [connector]: reregister }))
      setConnectMsg((m) => ({
        ...m,
        [connector]: res.connected
          ? 'Connected ✓'
          : reregister
            ? 'Almost there — we couldn’t finish setting up order sync. Tap “Re-register” to retry.'
            : `Not connected yet (${res.detail})`,
      }))
    })
  }

  return (
    <div data-onboard-wizard>
      {/* Step 1: Review & Confirm */}
      <section data-wizard-step="review" className="space-y-2">
        <h2 className="text-lg font-medium">1. Confirm your details</h2>
        {!draft.exists && (
          <p data-wizard-draft-empty>
            We couldn&apos;t draft your profile from public sources yet — fill these in.
          </p>
        )}
        <ul>
          {WIZARD_EDITABLE_FIELDS.map((field) => (
            <li key={field} data-field={field} className="py-1">
              <span className="font-medium">{FIELD_LABEL[field]}: </span>
              {editing === field ? (
                <>
                  <input
                    aria-label={FIELD_LABEL[field]}
                    value={fields[field]}
                    onChange={(e) => setFields((f) => ({ ...f, [field]: e.target.value }))}
                  />
                  <button type="button" disabled={pending} onClick={() => saveField(field)}>
                    Save
                  </button>
                  <button type="button" disabled={pending} onClick={() => setEditing(null)}>
                    Cancel
                  </button>
                </>
              ) : (
                <>
                  <span data-field-value>{fields[field] || <em>(empty)</em>}</span>
                  <button type="button" disabled={pending} onClick={() => setEditing(field)}>
                    Edit
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
        {saveMsg && <p data-wizard-save-msg>{saveMsg}</p>}
      </section>

      {/* Step 2: Connect data sources (system-browser handoff) */}
      <section data-wizard-step="connect" className="space-y-3">
        <h2 className="text-lg font-medium">2. Connect your tools</h2>
        {(['whatsapp', 'google_sheet', 'shopify'] as WizardConnector[]).map((connector) => (
          <div key={connector} data-connect={connector} className="py-1">
            <span className="font-medium">
              {CONNECTOR_LABEL[connector]}:{' '}
            </span>
            {connected[connector] ? (
              <span data-connected="true">Connected ✓</span>
            ) : (
              <>
                {/* VT-422 GAP-3: Shopify needs the owner's *.myshopify.com domain. The
                    one UI addition; sheets/whatsapp have no such input. */}
                {connector === 'shopify' && !authUrls[connector] && (
                  <input
                    type="text"
                    aria-label="Your Shopify store domain"
                    placeholder="your-store.myshopify.com"
                    value={shopDomain}
                    disabled={pending}
                    data-shop-domain-input
                    onChange={(e) => setShopDomain(e.target.value)}
                  />
                )}{' '}
                {!authUrls[connector] ? (
                  <button
                    type="button"
                    disabled={pending || (connector === 'shopify' && !shopDomain.trim())}
                    onClick={() => beginConnect(connector)}
                  >
                    Connect
                  </button>
                ) : (
                  <>
                    {/* Tappable handoff — opens the SYSTEM browser, never a JS popup. */}
                    <a href={authUrls[connector]} target="_blank" rel="noopener noreferrer" data-connect-link>
                      Open in your browser →
                    </a>{' '}
                    <button type="button" disabled={pending} onClick={() => recheck(connector)}>
                      I&apos;ve connected
                    </button>
                  </>
                )}
                {connectMsg[connector] && <span data-connect-msg> {connectMsg[connector]}</span>}
                {/* Sweep #16 (VT-453): re-register recovery — re-fire the connect handoff so the
                    owner can complete order-sync registration instead of being silently stuck
                    connected-but-uningested. */}
                {needsReregister[connector] && (
                  <button
                    type="button"
                    data-reregister={connector}
                    disabled={pending || (connector === 'shopify' && !shopDomain.trim())}
                    onClick={() => beginConnect(connector)}
                  >
                    Re-register
                  </button>
                )}
              </>
            )}
          </div>
        ))}
      </section>
    </div>
  )
}
