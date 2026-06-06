/** VT-98 — social-proof section: honest empty-state behaviour + the Pillar-7 no-fabrication guard. */

import { describe, expect, it } from 'vitest'

import { socialProofState, type SocialProofData } from '@/app/(marketing)/team/social-proof'

const EMPTY: SocialProofData = { testimonials: [], logos: [], metrics: [], press: [] }

describe('VT-98 socialProofState', () => {
  it('empty Phase-1 data → testimonials/metrics show an HONEST placeholder; logos/press omitted (no empty boxes)', () => {
    expect(socialProofState(EMPTY)).toEqual({
      testimonials: 'placeholder',
      logos: 'omit',
      metrics: 'placeholder',
      press: 'omit',
    })
  })

  it('with content → every populated sub-section renders content', () => {
    const d: SocialProofData = {
      testimonials: [{ owner_name: 'A', business_type: 'cafe', locality: 'Pune', quote: 'q' }],
      logos: [{ name: 'L', src: '/l.png' }],
      metrics: [{ label: 'recovered', value: '₹1,00,000' }],
      press: [{ name: 'P', src: '/p.png' }],
    }
    expect(socialProofState(d)).toEqual({
      testimonials: 'content',
      logos: 'content',
      metrics: 'content',
      press: 'content',
    })
  })
})

describe('VT-98 Pillar 7 — no fabricated content ships', () => {
  it('the shipped data/social-proof.json has ZERO testimonials/metrics/logos/press at launch', async () => {
    const data = (await import('@/data/social-proof.json')).default as unknown as {
      testimonials: unknown[]
      metrics: unknown[]
      logos: unknown[]
      press: unknown[]
    }
    expect(data.testimonials).toHaveLength(0)
    expect(data.metrics).toHaveLength(0)
    expect(data.logos).toHaveLength(0)
    expect(data.press).toHaveLength(0)
  })
})
