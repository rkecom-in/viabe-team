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
    <section aria-label="social proof">
      <h2>{labels.heading}</h2>

      {s.testimonials === 'content' ? (
        <ul aria-label="customer testimonials" className="sp-testimonials">
          {data.testimonials.map((tt, i) => (
            <li key={i}>
              <blockquote>{tt.quote}</blockquote>
              <cite>
                {tt.owner_name} · {tt.business_type} · {tt.locality}
              </cite>
            </li>
          ))}
        </ul>
      ) : (
        <p className="sp-placeholder">{labels.testimonials_placeholder}</p>
      )}

      {s.logos === 'content' && (
        <ul aria-label="customer logos" className="sp-logos">
          {data.logos.map((logo, i) => (
            <li key={i}>
              <img src={logo.src} alt={logo.name} />
            </li>
          ))}
        </ul>
      )}

      {s.metrics === 'content' ? (
        <ul aria-label="aggregate results" className="sp-metrics">
          {data.metrics.map((m, i) => (
            <li key={i}>
              <span className="sp-metric-value">{m.value}</span>
              <span className="sp-metric-label">{m.label}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="sp-placeholder">{labels.metrics_placeholder}</p>
      )}

      {s.press === 'content' && (
        <ul aria-label="press coverage" className="sp-press">
          {data.press.map((p, i) => (
            <li key={i}>
              <img src={p.src} alt={p.name} />
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
