/** VT-267 PR-C — business_profile draft read + Review-&-Confirm save (allowlist, fail-safe). */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchProfileDraft, saveProfileEdits, WIZARD_EDITABLE_FIELDS } from '@/lib/onboard/profile'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function client(row: any) {
  const builder: any = {
    select: () => builder,
    eq: () => builder,
    maybeSingle: async () => row,
  }
  return { from: () => builder }
}

describe('VT-267 PR-C — fetchProfileDraft', () => {
  it('maps L1 attributes to the draft shape', async () => {
    const c = client({
      data: { attributes: { business_name: 'Asha Sarees', business_type: 'retail', preferred_language: 'hi', owner_curated_context: 'festival focus', archetype: 'IGNORED' } },
      error: null,
    })
    const d = await fetchProfileDraft('t1', c as never)
    expect(d.exists).toBe(true)
    expect(d.business_name).toBe('Asha Sarees')
    expect(d.preferred_language).toBe('hi')
    // non-editable L1 keys are not surfaced on the draft shape
    expect((d as unknown as Record<string, unknown>).archetype).toBeUndefined()
  })

  it('empty (not exists) when no L1 row', async () => {
    const d = await fetchProfileDraft('t1', client({ data: null, error: null }) as never)
    expect(d.exists).toBe(false)
    expect(d.business_name).toBe('')
  })

  it('no tenant → empty', async () => {
    const d = await fetchProfileDraft('', client({ data: null }) as never)
    expect(d.exists).toBe(false)
  })
})

describe('VT-267 PR-C — saveProfileEdits', () => {
  it('forwards only editable fields + 2xx → ok', async () => {
    const f = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) }))
    vi.stubGlobal('fetch', f)
    const r = await saveProfileEdits('t1', { business_name: 'X', owner_curated_context: 'Y' })
    expect(r.ok).toBe(true)
    const body = JSON.parse(((f.mock.calls[0] as any[])[1] as any).body)
    expect(body.attributes).toEqual({ business_name: 'X', owner_curated_context: 'Y' })
  })

  it('drops non-editable keys before forwarding', async () => {
    const f = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }))
    vi.stubGlobal('fetch', f)
    // @ts-expect-error — intentionally pass a non-editable key
    await saveProfileEdits('t1', { business_name: 'X', archetype: 'evil' })
    const body = JSON.parse(((f.mock.calls[0] as any[])[1] as any).body)
    expect(body.attributes).toEqual({ business_name: 'X' })
    expect(body.attributes.archetype).toBeUndefined()
  })

  it('no editable fields → no_changes, no fetch', async () => {
    const f = vi.fn()
    vi.stubGlobal('fetch', f)
    const r = await saveProfileEdits('t1', {})
    expect(r.ok).toBe(false)
    expect(r.reason).toBe('no_changes')
    expect(f).not.toHaveBeenCalled()
  })

  it('non-2xx → http_<n>', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 400, json: async () => ({}) })))
    const r = await saveProfileEdits('t1', { business_name: 'X' })
    expect(r.ok).toBe(false)
    expect(r.reason).toBe('http_400')
  })
})

describe('VT-267 PR-C — WebView correctness (no JS popup OAuth)', () => {
  it('wizard component never uses window.open (popups blocked in WhatsApp WebView)', () => {
    const src = readFileSync(
      fileURLToPath(new URL('../../components/onboard/onboarding-wizard.tsx', import.meta.url)),
      'utf-8',
    )
    expect(src).not.toContain('window.open(') // no JS popup CALL (the comment may mention it)
    // OAuth handoff is a tappable anchor that opens the system browser.
    expect(src).toContain('target="_blank"')
    expect(src).toContain('rel="noopener noreferrer"')
  })

  it('editable field set matches the orchestrator allowlist', () => {
    expect([...WIZARD_EDITABLE_FIELDS].sort()).toEqual(
      ['business_name', 'business_type', 'owner_curated_context', 'preferred_language'],
    )
  })
})
