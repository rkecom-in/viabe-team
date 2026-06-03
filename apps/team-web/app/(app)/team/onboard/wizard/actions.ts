'use server'

/**
 * VT-267 PR-C — owner wizard server actions.
 *
 * Each action authenticates the owner session (requireFazal) before doing anything, and
 * resolves the tenant SERVER-SIDE from FAZAL_TENANT_ID (Phase-1; no client tenant field).
 * - saveProfileAction: persist Review-&-Confirm edits (MERGE via the orchestrator).
 * - startConnectAction: get the provider authorize URL to open in the SYSTEM browser (handoff).
 * - checkConnectionAction: the resume signal after the owner returns from the system browser.
 */

import { requireFazal } from '@/lib/auth/require-fazal'
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

function _tenantId(): string {
  return process.env.FAZAL_TENANT_ID ?? ''
}

export async function saveProfileAction(
  edits: Partial<Record<EditableField, string>>,
): Promise<SaveProfileResult> {
  await requireFazal()
  return saveProfileEdits(_tenantId(), edits)
}

export async function startConnectAction(
  connector: WizardConnector,
): Promise<StartConnectResult> {
  await requireFazal()
  return startConnect(_tenantId(), connector)
}

export async function checkConnectionAction(
  connector: WizardConnector,
): Promise<ConnectionStatus> {
  await requireFazal()
  return checkConnection(_tenantId(), connector)
}
