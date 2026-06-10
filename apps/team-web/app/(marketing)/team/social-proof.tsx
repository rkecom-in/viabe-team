/** VT-98 — social-proof section (landing). HONEST placeholders (Pillar 7 — never fabricated
 * testimonials/metrics). One component renders all 4 sub-sections (Pillar 8), data-driven from
 * data/social-proof.json. Empty sub-sections render an honest placeholder (testimonials/metrics)
 * or NOTHING at all (logos/press) — never an empty box. a11y: section/list aria-labels, logo
 * alt text; the empty state is communicated as text, not a blank region. */

export interface Testimonial {
  owner_name: string
  business_type: string
  locality: string
  quote: string
  photo?: string
}
export interface Logo {
  name: string
  src: string
}
export interface Metric {
  label: string
  value: string
}

export interface SocialProofData {
  testimonials: Testimonial[]
  logos: Logo[]
  metrics: Metric[]
  press: Logo[]
}

export interface SocialProofLabels {
  heading: string
  testimonials_placeholder: string
  metrics_placeholder: string
}

export type SectionState = 'content' | 'placeholder' | 'omit'

/** Pure (node-testable, no DOM): how each sub-section renders given the data. Testimonials +
 * metrics fall back to an honest placeholder when empty; logos + press are omitted entirely. */
export function socialProofState(d: SocialProofData): {
  testimonials: SectionState
  logos: SectionState
  metrics: SectionState
  press: SectionState
} {
  return {
    testimonials: d.testimonials.length ? 'content' : 'placeholder',
    logos: d.logos.length ? 'content' : 'omit',
    metrics: d.metrics.length ? 'content' : 'placeholder',
    press: d.press.length ? 'content' : 'omit',
  }
}

export function SocialProof({
  data,
  labels,
}: {
  data: SocialProofData
  labels: SocialProofLabels
}) {
  const s = socialProofState(data)
  return (
    <section aria-label="social proof" className="px-5 py-16">
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-8">
      <h2 className="text-center text-2xl font-bold tracking-tight text-gray-900 sm:text-3xl">
        {labels.heading}
      </h2>

      {s.testimonials === 'content' ? (
        <ul aria-label="customer testimonials" className="sp-testimonials grid gap-6 sm:grid-cols-2">
          {data.testimonials.map((tt, i) => (
            <li key={i} className="rounded-2xl border border-gray-200 bg-white p-6 shadow-sm">
              <blockquote className="leading-relaxed text-gray-700">{tt.quote}</blockquote>
              <cite className="mt-3 block text-sm not-italic text-gray-500">
                {tt.owner_name} · {tt.business_type} · {tt.locality}
              </cite>
            </li>
          ))}
        </ul>
      ) : (
        <p className="sp-placeholder rounded-xl border border-dashed border-gray-300 px-5 py-6 text-center text-sm italic text-gray-500">
          {labels.testimonials_placeholder}
        </p>
      )}

      {s.logos === 'content' && (
        <ul aria-label="customer logos" className="sp-logos flex flex-wrap items-center justify-center gap-8">
          {data.logos.map((logo, i) => (
            <li key={i}>
              <img src={logo.src} alt={logo.name} />
            </li>
          ))}
        </ul>
      )}

      {s.metrics === 'content' ? (
        <ul aria-label="aggregate results" className="sp-metrics flex flex-wrap justify-center gap-10 text-center">
          {data.metrics.map((m, i) => (
            <li key={i} className="flex flex-col">
              <span className="sp-metric-value text-3xl font-extrabold text-emerald-700">{m.value}</span>
              <span className="sp-metric-label text-sm text-gray-500">{m.label}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="sp-placeholder rounded-xl border border-dashed border-gray-300 px-5 py-6 text-center text-sm italic text-gray-500">
          {labels.metrics_placeholder}
        </p>
      )}

      {s.press === 'content' && (
        <ul aria-label="press coverage" className="sp-press flex flex-wrap items-center justify-center gap-8">
          {data.press.map((p, i) => (
            <li key={i}>
              <img src={p.src} alt={p.name} />
            </li>
          ))}
        </ul>
      )}
      </div>
    </section>
  )
}
