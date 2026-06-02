/**
 * VT-295 — Assignment management: VTAdmin gate (fail-closed), target validation,
 * server-side unassign resolution (IDOR rule), ops_audit writes.
 */

import { describe, expect, it, vi } from 'vitest'

import { OperatorRole } from '@/lib/auth/roles'
import {
  assignBusiness,
  fetchAllBusinesses,
  fetchAssignableOperators,
  unassignBusiness,
} from '@/lib/ops/assignment-admin'

const ADMIN = { operatorId: 'admin-1', role: OperatorRole.VTADMIN, assignedTenants: null }
const VTR = { operatorId: 'vtr-1', role: OperatorRole.VTR, assignedTenants: ['t1'] }

/** A programmable supabase-like client. `handler(table, op, filtersOrRow)` returns the
 *  result; list selects + maybeSingle + insert + update().eq() all route through it. */
function client(handler: (table: string, op: string, arg: any) => any) {
  const from = (table: string) => {
    const filters: Record<string, unknown> = {}
    const builder: any = {
      select: () => builder,
      order: () => builder,
      neq: () => builder,
      limit: () => builder,
      in: () => builder,
      eq: (col: string, val: unknown) => {
        filters[col] = val
        return builder
      },
      is: (col: string, val: unknown) => {
        filters[`${col}__is`] = val
        return builder
      },
      maybeSingle: async () => handler(table, 'maybeSingle', filters),
      insert: async (row: Record<string, unknown>) => handler(table, 'insert', row),
      update: (patch: Record<string, unknown>) => ({
        eq: async (col: string, val: unknown) => handler(table, 'update', { patch, [col]: val }),
      }),
      then: (resolve: any, reject: any) =>
        Promise.resolve(handler(table, 'select', filters)).then(resolve, reject),
    }
    return builder
  }
  return { from }
}

describe('VT-295 — fetchAllBusinesses', () => {
  it('VTR → [] (fail-closed, VTAdmin-only read)', async () => {
    const out = await fetchAllBusinesses(VTR, client(() => ({ data: [] })) as never)
    expect(out).toEqual([])
  })

  it('VTAdmin → businesses grouped with their active assignments', async () => {
    const c = client((table) => {
      if (table === 'tenants')
        return { data: [{ id: 't1', business_name: 'Asha' }, { id: 't2', business_name: 'Bose' }], error: null }
      // operator_assignments
      return {
        data: [
          { id: 'a1', tenant_id: 't1', operator_id: 'op-a' },
          { id: 'a2', tenant_id: 't1', operator_id: 'op-b' },
        ],
        error: null,
      }
    })
    const out = await fetchAllBusinesses(ADMIN, c as never)
    expect(out).toHaveLength(2)
    const t1 = out.find((b) => b.tenant_id === 't1')!
    expect(t1.business_name).toBe('Asha')
    expect(t1.assignments.map((a) => a.operator_id).sort()).toEqual(['op-a', 'op-b'])
    expect(t1.assignments.map((a) => a.assignment_id).sort()).toEqual(['a1', 'a2'])
    expect(out.find((b) => b.tenant_id === 't2')!.assignments).toEqual([])
  })
})

describe('VT-295 — fetchAssignableOperators', () => {
  it('VTR → [] (fail-closed)', async () => {
    const out = await fetchAssignableOperators(VTR, client(() => ({ data: [] })) as never)
    expect(out).toEqual([])
  })
  it('VTAdmin → active operators (bare UUIDs)', async () => {
    const c = client(() => ({ data: [{ user_id: 'op-a' }, { user_id: 'op-b' }], error: null }))
    const out = await fetchAssignableOperators(ADMIN, c as never)
    expect(out).toEqual([{ operator_id: 'op-a' }, { operator_id: 'op-b' }])
  })
})

describe('VT-295 — assignBusiness', () => {
  function spyClient(overrides: Partial<Record<string, any>> = {}) {
    const inserts: { table: string; row: any }[] = []
    const defaults: Record<string, any> = {
      tenantSingle: { data: { id: 't1' } },
      operatorSingle: { data: { user_id: 'op-a' } },
      existingActive: { data: null },
      ...overrides,
    }
    const c = client((table, op, arg) => {
      if (op === 'insert') {
        inserts.push({ table, row: arg })
        return { error: null }
      }
      if (table === 'tenants') return defaults.tenantSingle
      if (table === 'operator_allowlist') return defaults.operatorSingle
      if (table === 'operator_assignments') return defaults.existingActive
      return { data: null }
    })
    return { c, inserts }
  }

  it('VTR caller → rejected, NO write (VTAdmin-only)', async () => {
    const { c, inserts } = spyClient()
    const res = await assignBusiness(VTR, 't1', 'op-a', null, c as never)
    expect(res.ok).toBe(false)
    expect(res.reason).toMatch(/VTAdmin/)
    expect(inserts).toHaveLength(0)
  })

  it('unknown tenant → rejected, no write', async () => {
    const { c, inserts } = spyClient({ tenantSingle: { data: null } })
    const res = await assignBusiness(ADMIN, 'bad', 'op-a', null, c as never)
    expect(res.ok).toBe(false)
    expect(res.reason).toMatch(/unknown tenant/)
    expect(inserts).toHaveLength(0)
  })

  it('unknown/revoked operator → rejected, no write', async () => {
    const { c, inserts } = spyClient({ operatorSingle: { data: null } })
    const res = await assignBusiness(ADMIN, 't1', 'gone', null, c as never)
    expect(res.ok).toBe(false)
    expect(res.reason).toMatch(/operator/)
    expect(inserts).toHaveLength(0)
  })

  it('success → inserts assignment (assigned_by = caller) + ops_audit', async () => {
    const { c, inserts } = spyClient()
    const res = await assignBusiness(ADMIN, 't1', 'op-a', 'note', c as never)
    expect(res.ok).toBe(true)
    const assign = inserts.find((i) => i.table === 'operator_assignments')!
    expect(assign.row).toMatchObject({ tenant_id: 't1', operator_id: 'op-a', assigned_by: 'admin-1' })
    const audit = inserts.find((i) => i.table === 'ops_audit')!
    expect(audit.row).toMatchObject({ action: 'assign', target_kind: 'assignment', operator_id: 'admin-1', tenant_id: 't1' })
  })

  it('already-assigned → idempotent, no duplicate insert', async () => {
    const { c, inserts } = spyClient({ existingActive: { data: { id: 'a1' } } })
    const res = await assignBusiness(ADMIN, 't1', 'op-a', null, c as never)
    expect(res.ok).toBe(true)
    expect(res.reason).toMatch(/already assigned/)
    expect(inserts).toHaveLength(0)
  })
})

describe('VT-295 — unassignBusiness (IDOR rule: resolve target from the row)', () => {
  function spyClient(row: any) {
    const inserts: { table: string; row: any }[] = []
    const updates: any[] = []
    const c = client((table, op, arg) => {
      if (op === 'insert') {
        inserts.push({ table, row: arg })
        return { error: null }
      }
      if (op === 'update') {
        updates.push(arg)
        return { error: null }
      }
      if (table === 'operator_assignments') return { data: row }
      return { data: null }
    })
    return { c, inserts, updates }
  }

  it('VTR caller → rejected, no write', async () => {
    const { c, updates, inserts } = spyClient({ id: 'a1', operator_id: 'op-a', tenant_id: 't1', unassigned_at: null })
    const res = await unassignBusiness(VTR, 'a1', null, c as never)
    expect(res.ok).toBe(false)
    expect(res.reason).toMatch(/VTAdmin/)
    expect(updates).toHaveLength(0)
    expect(inserts).toHaveLength(0)
  })

  it('unknown assignment id → rejected', async () => {
    const { c, updates } = spyClient(null)
    const res = await unassignBusiness(ADMIN, 'missing', null, c as never)
    expect(res.ok).toBe(false)
    expect(res.reason).toMatch(/not found/)
    expect(updates).toHaveLength(0)
  })

  it('already-unassigned → rejected', async () => {
    const { c, updates } = spyClient({ id: 'a1', operator_id: 'op-a', tenant_id: 't1', unassigned_at: '2026-01-01' })
    const res = await unassignBusiness(ADMIN, 'a1', null, c as never)
    expect(res.ok).toBe(false)
    expect(res.reason).toMatch(/already unassigned/)
    expect(updates).toHaveLength(0)
  })

  it('success → audits the SERVER-RESOLVED tenant/operator, not a client field', async () => {
    // The row says tenant t9 / operator op-z; the caller only passed the assignment id.
    const { c, inserts, updates } = spyClient({ id: 'a1', operator_id: 'op-z', tenant_id: 't9', unassigned_at: null })
    const res = await unassignBusiness(ADMIN, 'a1', 'note', c as never)
    expect(res.ok).toBe(true)
    expect(updates).toHaveLength(1)
    const audit = inserts.find((i) => i.table === 'ops_audit')!
    expect(audit.row).toMatchObject({
      action: 'unassign',
      target_kind: 'assignment',
      tenant_id: 't9', // resolved from the row, not supplied by the caller
      target_id: 'op-z',
      operator_id: 'admin-1',
    })
  })
})
