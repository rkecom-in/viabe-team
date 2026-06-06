import Link from 'next/link'

import { getLandingDictionary, resolveLocale, t } from '@/lib/i18n'
import { launchMode } from '@/lib/launch-mode'
import { planPrices } from '@/lib/team-pricing'

import { FoundingCounterWidget, type FoundingStatus } from './founding-counter-widget'
import { WaitlistForm } from './waitlist-form'

/**
 * VT-95 — Viabe Team public landing page (bilingual EN + HI).
 *
 * Server-rendered. Locale = `?lang=en|hi` (the dashboard pattern); copy is the
 * `team-landing` dict (locales/team-landing/{en,hi}.json — NEEDS-FAZAL final copy). Prices
 * come from config (lib/team-pricing → NEXT_PUBLIC_*_PRICE_INR), never a literal (Pillar 7).
 * The founding-counter widget is server-seeded (VT-99) then re-fetches every 60s.
 *
 * NOT public until ENABLE_PUBLIC_SIGNUP go-live (Fazal + VT-329 i18n gate). The legal footer
 * pages (privacy / dpdp / terms / contact) + the final FAQ/hero copy are NEEDS-FAZAL.
 */
export const dynamic = 'force-dynamic'
export const revalidate = 300 // 5-min CDN cache (VT-95)

async function fetchFoundingStatus(): Promise<FoundingStatus | null> {
  const base = process.env.TEAM_ORCHESTRATOR_URL ?? 'http://localhost:8001'
  try {
    const res = await fetch(`${base}/api/team/founding-status`, { next: { revalidate: 60 } })
    if (!res.ok) return null
    return (await res.json()) as FoundingStatus
  } catch {
    return null // the widget degrades to "Loading availability…"
  }
}

const VALUE_CARDS = [1, 2, 3] as const
const FAQS = [1, 2, 3, 4, 5, 6, 7, 8] as const
const FEATURES = ['recovery', 'ledger', 'reports', 'support', 'day39'] as const

export default async function TeamLandingPage({
  searchParams,
}: {
  searchParams: Promise<{ lang?: string }>
}) {
  const { lang } = await searchParams
  const locale = resolveLocale(lang)
  const d = getLandingDictionary(locale)

  // VT-97 — one toggle picks the rendering tree (Pillar 8). maintenance → just the notice.
  const mode = launchMode()
  if (mode === 'maintenance') {
    return (
      <main lang={locale} className="maintenance">
        <h1>{t(d, 'maintenance.title')}</h1>
        <p>{t(d, 'maintenance.body')}</p>
      </main>
    )
  }

  const initial = await fetchFoundingStatus()
  const prices = planPrices()

  return (
    <main lang={locale}>
      <header>
        <span>{t(d, 'brand')}</span>
        <nav aria-label="language">
          <Link href="?lang=en">{t(d, 'lang.en')}</Link>
          {' | '}
          <Link href="?lang=hi">{t(d, 'lang.hi')}</Link>
        </nav>
      </header>

      <section aria-label="hero">
        <h1>{t(d, 'hero.title')}</h1>
        <p>{t(d, 'hero.subtitle')}</p>
        {mode === 'waitlist' ? (
          <WaitlistForm
            labels={{
              notice: t(d, 'waitlist.notice'),
              email: t(d, 'waitlist.email'),
              phone: t(d, 'waitlist.phone'),
              consent: t(d, 'waitlist.consent'),
              submit: t(d, 'waitlist.submit'),
              submitted: t(d, 'waitlist.submitted'),
              error: t(d, 'waitlist.error'),
            }}
          />
        ) : (
          <Link href="/team/signup" data-testid="hero-cta">
            {t(d, 'hero.cta')}
          </Link>
        )}
        <FoundingCounterWidget initial={initial} />
      </section>

      <section aria-label="value">
        <h2>{t(d, 'value.title')}</h2>
        {VALUE_CARDS.map((n) => (
          <article key={n}>
            <h3>{t(d, `value.${n}.title`)}</h3>
            <p>{t(d, `value.${n}.body`)}</p>
          </article>
        ))}
      </section>

      <section aria-label="pricing">
        <h2>{t(d, 'pricing.title')}</h2>
        {prices.map((p) => (
          <article key={p.tier} data-tier={p.tier}>
            <h3>{t(d, `pricing.${p.tier}.name`)}</h3>
            <p>{t(d, `pricing.${p.tier}.tagline`)}</p>
            <p>
              <span data-testid={`price-${p.tier}`}>₹{p.inr}</span> {t(d, 'pricing.period')}
            </p>
            <ul>
              {FEATURES.map((f) => (
                <li key={f}>{t(d, `pricing.feature.${f}`)}</li>
              ))}
            </ul>
            <Link href={`/team/signup?plan=${p.tier}`}>{t(d, 'pricing.cta')}</Link>
          </article>
        ))}
      </section>

      <section aria-label="day39">
        <h2>{t(d, 'day39.title')}</h2>
        <p>{t(d, 'day39.body')}</p>
      </section>

      <section aria-label="faq">
        <h2>{t(d, 'faq.title')}</h2>
        {FAQS.map((n) => (
          <details key={n}>
            <summary>{t(d, `faq.q${n}`)}</summary>
            <p>{t(d, `faq.a${n}`)}</p>
          </details>
        ))}
      </section>

      <footer>
        <p>{t(d, 'footer.tagline')}</p>
        <nav aria-label="legal">
          {/* NEEDS-FAZAL: the legal pages themselves are a follow-up (not live pre-launch). */}
          <Link href="/team/privacy">{t(d, 'footer.privacy')}</Link>
          {' · '}
          <Link href="/team/dpdp">{t(d, 'footer.dpdpa')}</Link>
          {' · '}
          <Link href="/team/terms">{t(d, 'footer.terms')}</Link>
          {' · '}
          <Link href="/team/contact">{t(d, 'footer.contact')}</Link>
        </nav>
        <small>{t(d, 'footer.rights')}</small>
      </footer>
    </main>
  )
}
