/** VT-297 — Ops Console: connect your Telegram (link-code issuer). Any operator (VTR/VTAdmin). */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { TelegramLink } from '@/components/ops/telegram-link'

export const dynamic = 'force-dynamic'

export default async function OpsTelegramPage() {
  try {
    await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops/telegram')
    throw err
  }

  return (
    <main data-area="team-ops-telegram" className="p-6 space-y-4">
      <header>
        <h1 className="text-2xl font-semibold">Connect Telegram</h1>
      </header>
      <TelegramLink />
    </main>
  )
}
