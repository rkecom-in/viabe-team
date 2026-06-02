/** VT-295 — Ops Console V2 Assignment management (VTAdmin only). VT-290 contract. */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { isVtAdmin } from '@/lib/auth/roles'
import { AssignmentAdmin } from '@/components/ops/assignment-admin'
import {
  fetchAllBusinesses,
  fetchAssignableOperators,
  type AssignableOperator,
  type BusinessAssignment,
} from '@/lib/ops/assignment-admin'

export const dynamic = 'force-dynamic'

export default async function OpsAssignmentPage() {
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops/assignment')
    throw err
  }

  // VTAdmin-only. NOT dead-ended (VT-290 contract): a VTR stays in the console shell and
  // sees an explicit message + nav, not a 404.
  if (!isVtAdmin(operator.role)) {
    return (
      <main data-area="team-ops-assignment" className="p-6 space-y-4">
        <header>
          <h1 className="text-2xl font-semibold">Assignment</h1>
        </header>
        <p data-ops-forbidden>
          Assignment management is VTAdmin-only. Ask a VTAdmin to change business assignments.
        </p>
      </main>
    )
  }

  let businesses: BusinessAssignment[] = []
  let operators: AssignableOperator[] = []
  let error: string | null = null
  try {
    ;[businesses, operators] = await Promise.all([
      fetchAllBusinesses(operator),
      fetchAssignableOperators(operator),
    ])
  } catch (err) {
    error = err instanceof Error ? err.message : 'unknown error'
    console.error('OpsAssignmentPage: load failed', err)
  }

  return (
    <main data-area="team-ops-assignment" className="p-6 space-y-4">
      <header>
        <h1 className="text-2xl font-semibold">Assignment (all businesses)</h1>
      </header>
      {error ? (
        <p data-section-error>couldn&apos;t load: {error}</p>
      ) : (
        <AssignmentAdmin businesses={businesses} operators={operators} />
      )}
    </main>
  )
}
