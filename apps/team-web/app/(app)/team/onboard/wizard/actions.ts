'use server'

/**
 * VT-267 PR-C — owner wizard server actions.
 *
 * VT-415 (owner-auth cutover): each action authenticates the OWNER session
 * (requireOwnerSession) AND resolves the tenant from that same session in one
 * call — the returned `tenantId` is the only tenant the action ever scopes to.
 * It is NEVER read from FAZAL_TENANT_ID and NEVER taken from a client argument
 * (the client `OnboardingWizard` passes only the connector / edits — no tenant
 * field; IDOR caught twice VT-293/294). A signed-in owner therefore reaches
 * ONLY their own tenant's onboarding draft/state.
 * - saveProfileAction: persist Review-&-Confirm edits (MERGE via the orchestrator).
 * - startConnectAction: get the provider authorize URL to open in the SYSTEM browser (handoff).
 * - checkConnectionAction: the resume signal after the owner returns from the system browser.
 */

import { requireOwnerSession } from '@/lib/auth/require-owner-session'
import {
  checkConnection,
  startConnect,
  type ConnectionStatus,
  type StartConnectResult,
  type WizardConnector,
} from '@/lib/onboard/connect'
import {
  saveProfileEdits,
  type EditableField,
  type SaveProfileResult,
} from '@/lib/onboard/profile'

export async function saveProfileAction(
  edits: Partial<Record<EditableField, string>>,
): Promise<SaveProfileResult> {
  const { tenantId } = await requireOwnerSession()
  return saveProfileEdits(tenantId, edits)
}

export async function startConnectAction(
  connector: WizardConnector,
  shop?: string,
): Promise<StartConnectResult> {
  // VT-415: tenant from the OWNER session only — never a client argument. VT-422
  // GAP-3: `shop` is the owner-typed *.myshopify.com domain for shopify (validated
  // orchestrator-side); the tenant is still session-resolved, not client-trusted.
  const { tenantId } = await requireOwnerSession()
  return startConnect(tenantId, connector, shop)
}

export async function checkConnectionAction(
  connector: WizardConnector,
): Promise<ConnectionStatus> {
  const { tenantId } = await requireOwnerSession()
  return checkConnection(tenantId, connector)
}
