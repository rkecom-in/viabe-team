/**
 * VT-267 PR-C — owner onboarding wizard.
 *
 * Renders inside the WhatsApp in-app browser (full-page, no popups). Steps: review the
 * draft business_profile (edit states) → connect Google Sheets + WhatsApp (OAuth hands off to
 * the SYSTEM browser, then re-check to resume).
 *
 * VT-415 (owner-auth cutover): gated on the OWNER session (requireOwnerSession); the tenant is
 * resolved SERVER-SIDE from that session — never FAZAL_TENANT_ID, never a client field.
 */

import { redirect } from 'next/navigation'

import { OwnerUnauthorizedError, requireOwnerSession } from '@/lib/auth/require-owner-session'
import { OnboardingWizard } from '@/components/onboard/onboarding-wizard'
import { checkConnection, type ConnectionStatus } from '@/lib/onboard/connect'
import { fetchProfileDraft, type ProfileDraft } from '@/lib/onboard/profile'

export const dynamic = 'force-dynamic'

export default async function OnboardWizardPage() {
  let tenantId: string
  try {
    ;({ tenantId } = await requireOwnerSession())
  } catch (err) {
    if (err instanceof OwnerUnauthorizedError) redirect('/team/login?next=/team/onboard/wizard')
    throw err
  }

  if (!tenantId) {
    return (
      <main data-area="onboard-wizard-degraded" className="p-6">
        <h1 className="text-2xl font-semibold">Onboarding unavailable</h1>
        <p>We couldn&apos;t resolve your business from your session. Sign in again.</p>
      </main>
    )
  }

  let draft: ProfileDraft
  let sheets: ConnectionStatus
  let whatsapp: ConnectionStatus
  try {
    ;[draft, sheets, whatsapp] = await Promise.all([
      fetchProfileDraft(tenantId),
      checkConnection(tenantId, 'google_sheet'),
      checkConnection(tenantId, 'whatsapp'),
    ])
  } catch (err) {
    return (
      <main data-area="onboard-wizard-error" className="p-6">
        <h1 className="text-2xl font-semibold">Onboarding</h1>
        <p data-section-error>couldn&apos;t load: {err instanceof Error ? err.message : 'unknown'}</p>
      </main>
    )
  }

  return (
    <main data-area="onboard-wizard" className="p-4 space-y-4">
      <header>
        <h1 className="text-2xl font-semibold">Set up your business</h1>
      </header>
      <OnboardingWizard
        draft={draft}
        initialSheets={sheets.connected}
        initialWhatsapp={whatsapp.connected}
      />
    </main>
  )
}
