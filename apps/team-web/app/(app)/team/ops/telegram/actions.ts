'use server'

/**
 * VT-297 — link-code issuer action. The operator is the AUTHENTICATED caller (requireOpsOperator);
 * the code is minted onto that operator_id server-side — a VTR can only ever mint a code for
 * themselves (no operator field crosses the wire).
 */

import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { mintLinkCode, type MintResult } from '@/lib/telegram/issuer'

export async function generateLinkCodeAction(): Promise<MintResult> {
  const operator = await requireOpsOperator()
  return mintLinkCode(operator.operatorId)
}
