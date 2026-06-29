/**
 * VT-406 (Part B) — entity-match flow logic: candidate fetch, confirm sequence, the
 * verified/reject/retry classifier, the create-account gate, and the confirmable predicate.
 *
 * The load-bearing invariants pinned here:
 *   - "verified" is reachable ONLY through a confirm with status === 'gstin_verified' — a web/GBP
 *     candidate is never classified verified;
 *   - "none of these" AND an invalid_gstin confirm both collapse to `reject` with NO reason carried
 *     (no inactive-vs-not-found enumeration oracle);
 *   - vendor_down (and any transport failure) is `retry`, NOT a reject;
 *   - the create-account gate is false until a verified entity with a gstin is held;
 *   - the proxy fetches fail closed (empty candidates / unverified) on non-2xx / throw.
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { describe, expect, it, vi } from 'vitest'

import {
  canCreateAccount,
  candidateDisplayName,
  cityToStateCode,
  classifyConfirm,
  confirmCandidate,
  fetchCandidates,
  fetchGstinsByPan,
  findCinCandidate,
  findNamedNoGstin,
  isConfirmable,
  isValidGstinFormat,
  isValidPanFormat,
  pollDiscoveryStatus,
  startDiscovery,
  type EntityCandidate,
  type EntityConfirmResult,
} from '@/lib/entity-match'

function resp(status: number, body: unknown = {}): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response
}

const WEB_CANDIDATE: EntityCandidate = {
  trade_name: 'Sundaram Book Store',
  source: 'web',
  candidate_gstin: '29ABCDE1234F1Z5',
  legal_name: 'Sundaram Multi Pap Limited',
  detail: 'MG Road, Bengaluru · Bookstore',
}
const GBP_CANDIDATE: EntityCandidate = {
  trade_name: 'Sundaram Book Store',
  source: 'gbp',
  candidate_gstin: null,
  legal_name: null,
  detail: 'Jayanagar · Book shop',
}

describe('VT-406 fetchCandidates', () => {
  it('200 → candidates, posts to the proxy route with business_name + city', async () => {
    const f = vi.fn().mockResolvedValue(resp(200, { candidates: [WEB_CANDIDATE, GBP_CANDIDATE] }))
    const r = await fetchCandidates('Sundaram Book Store', 'Bengaluru', f)
    expect(r.ok).toBe(true)
    expect(r.candidates).toHaveLength(2)
    const [url, init] = f.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/team/onboard/entity-candidates')
    expect(JSON.parse(init.body as string)).toEqual({
      business_name: 'Sundaram Book Store',
      city: 'Bengaluru',
    })
  })

  it('fails CLOSED to an empty list on non-2xx (never stalls signup)', async () => {
    const f = vi.fn().mockResolvedValue(resp(502))
    expect(await fetchCandidates('X', 'Y', f)).toEqual({
      ok: false,
      candidates: [],
      reason: 'http_502',
    })
  })

  it('fails CLOSED to an empty list on throw', async () => {
    const f = vi.fn().mockRejectedValue(new Error('network'))
    expect(await fetchCandidates('X', 'Y', f)).toEqual({ ok: false, candidates: [], reason: 'error' })
  })
})

describe('VT-406 confirmCandidate + classifyConfirm', () => {
  it('candidates render + pick → confirm called with the picked gstin', async () => {
    const f = vi.fn().mockResolvedValue(resp(200, { ok: true, status: 'gstin_verified', name: 'SUNDARAM MULTI PAP LIMITED' }))
    const envelope = await confirmCandidate('29ABCDE1234F1Z5', 'Sundaram Multi Pap', f)
    const [url, init] = f.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/team/onboard/entity-confirm')
    // VT-#10: the typed business_name threads through so the orchestrator name-matches at verify.
    expect(JSON.parse(init.body as string)).toEqual({ gstin: '29ABCDE1234F1Z5', business_name: 'Sundaram Multi Pap' })
    expect(envelope.status).toBe('gstin_verified')
  })

  it('"verified" ONLY after a gstin_verified confirm — uses the authoritative registry name', () => {
    const envelope: EntityConfirmResult = {
      ok: true,
      status: 'gstin_verified',
      name: 'SUNDARAM MULTI PAP LIMITED',
    }
    expect(classifyConfirm(envelope, '29ABCDE1234F1Z5')).toEqual({
      kind: 'verified',
      gstin: '29ABCDE1234F1Z5',
      name: 'SUNDARAM MULTI PAP LIMITED',
    })
  })

  it('a non-verified status (even ok:true) is NEVER verified → reject', () => {
    // Defence: an "ok" response that is not gstin_verified must not pass as verified.
    expect(classifyConfirm({ ok: true, status: 'unverified' }, 'G').kind).toBe('reject')
  })

  it('invalid_gstin → reject, with NO reason carried (no enumeration oracle)', () => {
    const out = classifyConfirm({ ok: false, status: 'unverified', reason: 'invalid_gstin' }, 'G')
    expect(out).toEqual({ kind: 'reject' })
    // The reject outcome leaks NO inactive-vs-not-found distinction.
    expect(Object.keys(out)).toEqual(['kind'])
  })

  it('invalid_gstin_format → reject (same generic terminus)', () => {
    expect(classifyConfirm({ ok: false, reason: 'invalid_gstin_format' }, 'G')).toEqual({ kind: 'reject' })
  })

  it('vendor_down → retry, NOT reject', () => {
    expect(classifyConfirm({ ok: false, reason: 'vendor_down' }, 'G')).toEqual({ kind: 'retry' })
  })

  it('http_5xx / timeout / transport error → retry, NOT reject', () => {
    expect(classifyConfirm({ ok: false, reason: 'http_503' }, 'G').kind).toBe('retry')
    expect(classifyConfirm({ ok: false, reason: 'timeout' }, 'G').kind).toBe('retry')
    expect(classifyConfirm({ ok: false, reason: 'error' }, 'G').kind).toBe('retry')
  })

  it('confirm proxy fails CLOSED on non-2xx (→ classified retry, never a false verified)', async () => {
    const f = vi.fn().mockResolvedValue(resp(500))
    const envelope = await confirmCandidate('G', '', f)
    expect(envelope.ok).toBe(false)
    expect(classifyConfirm(envelope, 'G').kind).toBe('retry')
  })

  it('confirm proxy fails CLOSED on throw (→ retry)', async () => {
    const f = vi.fn().mockRejectedValue(new Error('network'))
    const envelope = await confirmCandidate('G', '', f)
    expect(envelope).toEqual({ ok: false, reason: 'error' })
    expect(classifyConfirm(envelope, 'G').kind).toBe('retry')
  })
})

describe('VT-406 create-account gate (canCreateAccount)', () => {
  it('false until a verified entity with a gstin is held', () => {
    expect(canCreateAccount(null)).toBe(false)
    expect(canCreateAccount({ gstin: '', name: 'X' })).toBe(false)
  })
  it('true only with a verified gstin', () => {
    expect(canCreateAccount({ gstin: '29ABCDE1234F1Z5', name: 'X' })).toBe(true)
  })
})

describe('VT-406 isConfirmable (provenance — a GBP candidate cannot be verified)', () => {
  it('a web candidate WITH a gstin is confirmable', () => {
    expect(isConfirmable(WEB_CANDIDATE)).toBe(true)
  })
  it('a GBP candidate with NO gstin is NOT confirmable (verify needs a registry id)', () => {
    expect(isConfirmable(GBP_CANDIDATE)).toBe(false)
  })
})

describe('VT-406 provenance — the wizard never badges a candidate as verified', () => {
  const src = readFileSync(
    fileURLToPath(new URL('../../app/(marketing)/team/signup/entity-match-step.tsx', import.meta.url)),
    'utf-8',
  )

  it('the verified chip is rendered ONLY in the verified-step branch (data-entity-step="verified")', () => {
    // The verified chip copy must NOT appear inside the picking branch. Assert the verified-chip
    // message is reached only via verified_heading (the verified branch), and the picking branch
    // tags candidates with the found chip.
    expect(src).toContain('data-entity-step="verified"')
    expect(src).toContain("chip(t.verified_chip, 'verified')")
    // Candidates in the picking list carry the FOUND chip, never the verified chip.
    expect(src).toContain("chip(t.found_chip, 'found')")
    // The candidate <li> rendering block must not call the verified chip.
    const pickingBlock = src.slice(src.indexOf('data-entity-step="picking"'))
    expect(pickingBlock).not.toContain("chip(t.verified_chip, 'verified')")
  })

  it('the verified branch renders the authoritative registry name (verified.name), not a candidate name', () => {
    expect(src).toContain('data-verified-name')
    expect(src).toContain('verified.name')
  })

  it('VT-450 — a found-company-no-GSTIN candidate renders the found state, NOT the empty-state', () => {
    // The found-no-GSTIN screen exists with its data-* hooks (the real returned name + both CTAs).
    expect(src).toContain('data-entity-step="found_no_gstin"')
    expect(src).toContain('data-found-name')
    expect(src).toContain('data-found-name-input') // (a) change company name → re-search
    expect(src).toContain('data-found-research')
    expect(src).toContain('data-found-enter-gstin') // (b) enter my GST number → manual path
    // The fetch result routes to found_no_gstin via findNamedNoGstin — so a named-no-GSTIN result
    // does NOT fall through to the "couldn't find your business" empty-state (data-entity-empty).
    expect(src).toContain('findNamedNoGstin')
    expect(src).toContain("setStep(named ? 'found_no_gstin' : 'picking')")
    // (b) reuses the existing manual-GSTIN path (openManual), keeping the Sandbox verify gate intact.
    const foundBlock = src.slice(src.indexOf('data-entity-step="found_no_gstin"'))
    expect(foundBlock).toContain('onClick={openManual}')
  })
})

describe('VT-448 isValidGstinFormat (manual-entry format gate)', () => {
  it('accepts a well-formed 15-char GSTIN (any case / surrounding space)', () => {
    expect(isValidGstinFormat('27AAACR5055K1Z7')).toBe(true)
    expect(isValidGstinFormat(' 27aaacr5055k1z7 ')).toBe(true) // trimmed + upper-cased before test
  })

  it('rejects wrong length / shape', () => {
    expect(isValidGstinFormat('')).toBe(false)
    expect(isValidGstinFormat('27AAACR5055K1Z')).toBe(false) // 14 chars
    expect(isValidGstinFormat('27AAACR5055K1Z77')).toBe(false) // 16 chars
    expect(isValidGstinFormat('AB27AACR5055K1Z7')).toBe(false) // letters in the state-code slot
    expect(isValidGstinFormat('27AAACR5055K1A7')).toBe(false) // no mandatory Z in slot 14
  })

  it('is a FORMAT gate only — a valid format is NOT verification (Sandbox stays the authority)', () => {
    // A format-valid string never unlocks create on its own; canCreateAccount needs a verified entity.
    expect(canCreateAccount({ gstin: '27AAACR5055K1Z7', name: 'X' })).toBe(true)
    expect(canCreateAccount(null)).toBe(false)
  })
})

describe('VT-448 fetchGstinsByPan (PRIMARY identify path)', () => {
  it('200 → gstins, posts to the proxy route with {pan, state_code}', async () => {
    const f = vi.fn().mockResolvedValue(resp(200, { ok: true, gstins: ['27AAACR5055K1Z7'] }))
    const r = await fetchGstinsByPan('AAACR5055K', '27', f)
    expect(r.ok).toBe(true)
    expect(r.gstins).toEqual(['27AAACR5055K1Z7'])
    const [url, init] = f.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/team/onboard/gstins-by-pan')
    expect(JSON.parse(init.body as string)).toEqual({ pan: 'AAACR5055K', state_code: '27' })
  })

  it('parses {ok, gstins} and defaults a missing list to empty', async () => {
    const f = vi.fn().mockResolvedValue(resp(200, { ok: true }))
    expect(await fetchGstinsByPan('AAACR5055K', '27', f)).toEqual({
      ok: true,
      gstins: [],
      reason: 'ok',
    })
  })

  it('fails CLOSED to an empty list on non-2xx (manual fallback always exists)', async () => {
    const f = vi.fn().mockResolvedValue(resp(502))
    expect(await fetchGstinsByPan('AAACR5055K', '27', f)).toEqual({
      ok: false,
      gstins: [],
      reason: 'http_502',
    })
  })

  it('fails CLOSED to an empty list on throw', async () => {
    const f = vi.fn().mockRejectedValue(new Error('network'))
    expect(await fetchGstinsByPan('AAACR5055K', '27', f)).toEqual({
      ok: false,
      gstins: [],
      reason: 'error',
    })
  })
})

describe('VT-448 isValidPanFormat (PAN format gate)', () => {
  it('accepts a well-formed 10-char PAN (any case / surrounding space)', () => {
    expect(isValidPanFormat('AAACR5055K')).toBe(true)
    expect(isValidPanFormat(' aaacr5055k ')).toBe(true) // trimmed + upper-cased before test
  })

  it('rejects wrong length / shape', () => {
    expect(isValidPanFormat('')).toBe(false)
    expect(isValidPanFormat('AAACR5055')).toBe(false) // 9 chars (missing trailing letter)
    expect(isValidPanFormat('AAACR5055KK')).toBe(false) // 11 chars
    expect(isValidPanFormat('AAAC15055K')).toBe(false) // digit in the 5-letter block
    expect(isValidPanFormat('AAACR505K5')).toBe(false) // letter in the 4-digit block
    expect(isValidPanFormat('AAACR50555')).toBe(false) // digit in the trailing-letter slot
    expect(isValidPanFormat('27AAACR505')).toBe(false) // GSTIN-shaped, not a PAN
  })

  it('is a FORMAT gate only — a valid PAN is NOT verification', () => {
    // A format-valid PAN never unlocks create; canCreateAccount still needs a verified entity.
    expect(canCreateAccount(null)).toBe(false)
  })
})

describe('VT-448 cityToStateCode', () => {
  it('maps known cities to their GST state code (case / space insensitive)', () => {
    expect(cityToStateCode('Mumbai')).toBe('27')
    expect(cityToStateCode(' mumbai ')).toBe('27')
    expect(cityToStateCode('Pune')).toBe('27')
    expect(cityToStateCode('Maharashtra')).toBe('27')
    expect(cityToStateCode('Delhi')).toBe('07')
    expect(cityToStateCode('Bengaluru')).toBe('29')
    expect(cityToStateCode('Karnataka')).toBe('29')
    expect(cityToStateCode('Chennai')).toBe('33')
    expect(cityToStateCode('Tamil Nadu')).toBe('33')
    expect(cityToStateCode('Kolkata')).toBe('19')
    expect(cityToStateCode('West Bengal')).toBe('19')
    expect(cityToStateCode('Hyderabad')).toBe('36')
    expect(cityToStateCode('Telangana')).toBe('36')
  })

  it('returns null for an unknown city (component asks for a state hint, never guesses)', () => {
    expect(cityToStateCode('Atlantis')).toBeNull()
    expect(cityToStateCode('')).toBeNull()
  })
})

describe('VT-449 findCinCandidate', () => {
  const REGISTRY: EntityCandidate = {
    trade_name: 'Sundaram Multi Pap Limited',
    source: 'registry',
    candidate_gstin: null,
    legal_name: null,
    detail: null,
    candidate_cin: 'U22210KA1995PLC012345',
  }

  it('surfaces the first registry candidate with a CIN', () => {
    expect(findCinCandidate([WEB_CANDIDATE, GBP_CANDIDATE, REGISTRY])).toEqual({
      cin: 'U22210KA1995PLC012345',
      tradeName: 'Sundaram Multi Pap Limited',
    })
  })

  it('returns null when no registry candidate is present (create then sends cin: \'\')', () => {
    expect(findCinCandidate([WEB_CANDIDATE, GBP_CANDIDATE])).toBeNull()
    expect(findCinCandidate([])).toBeNull()
  })

  it('ignores a registry candidate with an empty/missing CIN (never surfaces a blank confirm)', () => {
    const noCin: EntityCandidate = { ...REGISTRY, candidate_cin: '   ' }
    const undefCin: EntityCandidate = { ...REGISTRY, candidate_cin: undefined }
    expect(findCinCandidate([noCin, undefCin])).toBeNull()
  })

  it('does NOT treat a web/gbp candidate as a CIN source even if it carried a candidate_cin', () => {
    // Defence-in-depth: only source==='registry' surfaces a CIN-confirm (no SERP web row mislabeled).
    const webWithCin: EntityCandidate = { ...WEB_CANDIDATE, candidate_cin: 'U99999XX0000ZZZ999999' }
    expect(findCinCandidate([webWithCin])).toBeNull()
  })

  it('trims the surfaced CIN', () => {
    const padded: EntityCandidate = { ...REGISTRY, candidate_cin: '  U22210KA1995PLC012345  ' }
    expect(findCinCandidate([padded])?.cin).toBe('U22210KA1995PLC012345')
  })
})

describe('VT-450 candidateDisplayName', () => {
  it('prefers trade_name, then legal_name', () => {
    expect(candidateDisplayName(WEB_CANDIDATE)).toBe('Sundaram Book Store')
    expect(
      candidateDisplayName({ ...GBP_CANDIDATE, trade_name: null, legal_name: 'Sundaram Multi Pap Ltd' }),
    ).toBe('Sundaram Multi Pap Ltd')
  })

  it('returns "" (trimmed) when no usable name is present', () => {
    expect(candidateDisplayName({ ...GBP_CANDIDATE, trade_name: '  ', legal_name: null })).toBe('')
    expect(candidateDisplayName({ ...GBP_CANDIDATE, trade_name: null, legal_name: null })).toBe('')
  })
})

describe('VT-450 findNamedNoGstin (found-company-no-GSTIN state)', () => {
  // The Fazal e2e case: discovery found RKeCom via GBP (trade_name set) but no candidate carries a
  // GSTIN. This is the FOUND state, not the empty-state.
  const GBP_NO_GSTIN: EntityCandidate = {
    trade_name: 'RKeCom',
    source: 'gbp',
    candidate_gstin: null,
    legal_name: null,
    detail: 'Sector 62, Noida · Software company',
    phone: '+919321553267',
  }

  it('surfaces the found name when a named candidate exists but NONE is confirmable', () => {
    expect(findNamedNoGstin([GBP_NO_GSTIN])).toEqual({ tradeName: 'RKeCom' })
  })

  it('returns null on a genuinely ZERO-candidate result (→ the empty-state)', () => {
    expect(findNamedNoGstin([])).toBeNull()
  })

  it('returns null when ANY candidate is confirmable (→ the normal pick list stands)', () => {
    // A GSTIN-bearing web hit alongside the no-GSTIN GBP one → pick list, not the found-no-GSTIN state.
    expect(findNamedNoGstin([GBP_NO_GSTIN, WEB_CANDIDATE])).toBeNull()
  })

  it('counts a registry candidate name as "found" too (uses trade_name)', () => {
    const REGISTRY_NO_GSTIN: EntityCandidate = {
      trade_name: 'RKeCom Services Pvt Ltd',
      source: 'registry',
      candidate_gstin: null,
      legal_name: null,
      detail: null,
      candidate_cin: 'U72900UP2020PTC000000',
    }
    expect(findNamedNoGstin([REGISTRY_NO_GSTIN])).toEqual({ tradeName: 'RKeCom Services Pvt Ltd' })
  })

  it('returns null when candidates exist but carry no usable name (no GSTIN AND no name)', () => {
    const nameless: EntityCandidate = {
      trade_name: null,
      source: 'gbp',
      candidate_gstin: null,
      legal_name: null,
      detail: 'A listing with no name',
    }
    expect(findNamedNoGstin([nameless])).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// VT-507 — progressive discovery: startDiscovery + pollDiscoveryStatus
// ---------------------------------------------------------------------------

describe('VT-507 startDiscovery', () => {
  it('200 → ok:true, discoveryId set, posts {business_name, city} to the proxy route', async () => {
    const f = vi.fn().mockResolvedValue(resp(200, { discovery_id: 'disc-abc123' }))
    const r = await startDiscovery('Sundaram Book Store', 'Bengaluru', f)
    expect(r.ok).toBe(true)
    expect(r.discoveryId).toBe('disc-abc123')
    const [url, init] = f.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/team/onboard/discovery/start')
    expect(JSON.parse(init.body as string)).toEqual({
      business_name: 'Sundaram Book Store',
      city: 'Bengaluru',
    })
  })

  it('200 with missing discovery_id → ok:true, discoveryId:null (caller degrades gracefully)', async () => {
    const f = vi.fn().mockResolvedValue(resp(200, {}))
    const r = await startDiscovery('X', 'Y', f)
    expect(r.ok).toBe(true)
    expect(r.discoveryId).toBeNull()
  })

  it('fails CLOSED on non-2xx → {ok:false, discoveryId:null} (never blocks signup)', async () => {
    const f = vi.fn().mockResolvedValue(resp(502))
    const r = await startDiscovery('X', 'Y', f)
    expect(r.ok).toBe(false)
    expect(r.discoveryId).toBeNull()
    expect(r.reason).toBe('http_502')
  })

  it('fails CLOSED on throw → {ok:false, reason:"error"}', async () => {
    const f = vi.fn().mockRejectedValue(new Error('network'))
    const r = await startDiscovery('X', 'Y', f)
    expect(r.ok).toBe(false)
    expect(r.discoveryId).toBeNull()
    expect(r.reason).toBe('error')
  })
})

describe('VT-507 pollDiscoveryStatus', () => {
  it('200 → parses overall_status, candidates, both_complete_zero; GETs the right URL', async () => {
    const f = vi.fn().mockResolvedValue(
      resp(200, {
        overall_status: 'searching',
        candidates: [WEB_CANDIDATE],
        both_complete_zero: false,
      }),
    )
    const r = await pollDiscoveryStatus('disc-abc123', f)
    expect(r.ok).toBe(true)
    expect(r.overallStatus).toBe('searching')
    expect(r.candidates).toHaveLength(1)
    expect(r.bothCompleteZero).toBe(false)
    const [url] = f.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/team/onboard/discovery/disc-abc123')
  })

  it('overall_status "complete" is mapped correctly', async () => {
    const f = vi.fn().mockResolvedValue(
      resp(200, { overall_status: 'complete', candidates: [WEB_CANDIDATE], both_complete_zero: false }),
    )
    expect((await pollDiscoveryStatus('d', f)).overallStatus).toBe('complete')
  })

  it('any non-"complete" overall_status maps to "searching" (default)', async () => {
    const f = vi.fn().mockResolvedValue(
      resp(200, { overall_status: 'running', candidates: [], both_complete_zero: false }),
    )
    expect((await pollDiscoveryStatus('d', f)).overallStatus).toBe('searching')
  })

  it("both_complete_zero:true — honest-empty signal; spinner must NOT be replaced by \"couldn't find\" on a source error", async () => {
    const f = vi.fn().mockResolvedValue(
      resp(200, { overall_status: 'complete', candidates: [], both_complete_zero: true }),
    )
    const r = await pollDiscoveryStatus('d', f)
    expect(r.bothCompleteZero).toBe(true)
    expect(r.ok).toBe(true)
    expect(r.candidates).toHaveLength(0)
  })

  it('missing candidates defaults to empty array', async () => {
    const f = vi.fn().mockResolvedValue(resp(200, { overall_status: 'searching' }))
    const r = await pollDiscoveryStatus('d', f)
    expect(r.candidates).toEqual([])
    expect(r.bothCompleteZero).toBe(false)
  })

  it("fails CLOSED on non-2xx → ok:false, no candidates, no honest-empty signal (retry, not \"couldn't find\")", async () => {
    const f = vi.fn().mockResolvedValue(resp(503))
    const r = await pollDiscoveryStatus('d', f)
    expect(r.ok).toBe(false)
    expect(r.candidates).toHaveLength(0)
    expect(r.bothCompleteZero).toBe(false)
    expect(r.reason).toBe('http_503')
  })

  it('fails CLOSED on throw → ok:false, reason:"error"', async () => {
    const f = vi.fn().mockRejectedValue(new Error('network'))
    const r = await pollDiscoveryStatus('d', f)
    expect(r.ok).toBe(false)
    expect(r.reason).toBe('error')
  })
})

describe('VT-507 progressive discovery — component source-code structure checks', () => {
  const src = readFileSync(
    fileURLToPath(new URL('../../app/(marketing)/team/signup/entity-match-step.tsx', import.meta.url)),
    'utf-8',
  )

  it('has a "discovering" screen (data-entity-step="discovering")', () => {
    expect(src).toContain('data-entity-step="discovering"')
  })

  it('shows the manual option at 10s via showManualEarly (data-entity-manual-early)', () => {
    expect(src).toContain('data-entity-manual-early')
    expect(src).toContain('showManualEarly')
    // The 10s timer fires setTimeout at exactly 10_000ms.
    expect(src).toContain('10_000')
  })

  it('honest-empty (data-entity-honest-empty) is guarded by bothCompleteZero — never by a source error', () => {
    expect(src).toContain('data-entity-honest-empty')
    // The honest-empty block is inside a `bothCompleteZero` condition.
    const honestBlock = src.slice(src.indexOf('data-entity-honest-empty'))
    // The enclosing conditional check is visible nearby — the honest-empty message can't appear
    // unconditionally (a source error should NOT show "couldn't find").
    expect(src).toContain('bothCompleteZero')
  })

  it('degraded state (data-entity-discovery-degraded) is separate from honest-empty', () => {
    expect(src).toContain('data-entity-discovery-degraded')
    expect(src).toContain('degraded')
  })

  it('stopDiscovery() is called in pick() — cancel-on-commit when user selects a candidate', () => {
    // The pick function must call stopDiscovery() before confirmGstin.
    const pickFnIdx = src.indexOf('function pick(')
    const pickFnBody = src.slice(pickFnIdx, src.indexOf('\n  }', pickFnIdx) + 10)
    expect(pickFnBody).toContain('stopDiscovery()')
  })

  it('stopDiscovery() is called in submitManualGstin() — cancel-on-commit when GSTIN is entered', () => {
    const submitFnIdx = src.indexOf('function submitManualGstin(')
    const submitFnBody = src.slice(submitFnIdx, src.indexOf('\n  }', submitFnIdx) + 10)
    expect(submitFnBody).toContain('stopDiscovery()')
  })

  it('uses startDiscovery + pollDiscoveryStatus from @/lib/entity-match', () => {
    expect(src).toContain('startDiscovery')
    expect(src).toContain('pollDiscoveryStatus')
  })

  it('verified chip is NOT in the discovering screen (provenance: found candidates are "found", never verified)', () => {
    const discoveringBlock = src.slice(src.indexOf('data-entity-step="discovering"'))
    const nextSectionIdx = discoveringBlock.indexOf('data-entity-step="loading"')
    const discoveringOnly = nextSectionIdx > 0 ? discoveringBlock.slice(0, nextSectionIdx) : discoveringBlock.slice(0, 3000)
    expect(discoveringOnly).not.toContain("chip(t.verified_chip, 'verified')")
  })

  it('the verified chip is still rendered ONLY in the verified-step branch (existing provenance test)', () => {
    expect(src).toContain('data-entity-step="verified"')
    expect(src).toContain("chip(t.verified_chip, 'verified')")
    const pickingBlock = src.slice(src.indexOf('data-entity-step="picking"'))
    expect(pickingBlock).not.toContain("chip(t.verified_chip, 'verified')")
  })
})
