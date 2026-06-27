import Link from 'next/link'

import { getLandingDictionary, resolveLocale, t } from '@/lib/i18n'
import { launchMode } from '@/lib/launch-mode'
import { planPrices } from '@/lib/team-pricing'

import socialProofData from '@/data/social-proof.json'

import { trackExperimentExposure } from '@/lib/analytics-events'
import { getExperiment } from '@/lib/experiments'

import { FoundingCounterWidget, type FoundingStatus } from './founding-counter-widget'
import { SocialProof, type SocialProofData } from './social-proof'
import { WaitlistForm } from './waitlist-form'

/**
 * VT-95 — Viabe Team public landing page (bilingual EN + HI).
 *
 * Server-rendered. Locale = `?lang=en|hi` (the dashboard pattern); copy is the
 * `team-landing` dict (locales/team-landing/{en,hi}.json — NEEDS-FAZAL final copy). Prices
 * come from config (lib/team-pricing → NEXT_PUBLIC_*_PRICE_INR), never a literal (Pillar 7).
 * The founding-counter widget is server-seeded (VT-99) then re-fetches every 60s.
 *
 * VT-372: the page shipped as semantic markup with NO styling (never a regression — no
 * stylesheet ever existed). Styled with Tailwind utilities (the repo's system): light theme,
 * emerald accent, mobile-first; copy/structure/logic untouched.
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
      <main
        lang={locale}
        className="maintenance flex min-h-screen flex-col items-center justify-center gap-4 bg-background px-6 text-center"
      >
        <h1 className="text-3xl font-bold tracking-tight text-foreground">
          {t(d, 'maintenance.title')}
        </h1>
        <p className="max-w-md text-muted-foreground">{t(d, 'maintenance.body')}</p>
      </main>
    )
  }

  const initial = await fetchFoundingStatus()
  const prices = planPrices()
  // VT-100 — cookie-free A/B: the example experiment is inactive (returns 'control') in Phase 1;
  // the framework is exercised end-to-end (assign → expose) for when real experiments run.
  const heroVariant = await getExperiment('homepage_hero_v1')
  trackExperimentExposure('homepage_hero_v1', heroVariant)

  return (
    <main lang={locale} className="min-h-screen bg-background text-foreground antialiased">
      <header className="mx-auto flex w-full max-w-5xl items-center justify-between px-5 py-5">
        <span className="text-lg font-bold tracking-tight text-primary">{t(d, 'brand')}</span>
        <nav aria-label="language" className="text-sm text-muted-foreground">
          <Link href="?lang=en" className="rounded px-2 py-1 hover:bg-muted hover:text-foreground">
            {t(d, 'lang.en')}
          </Link>
          <span aria-hidden className="text-border">
            |
          </span>
          <Link href="?lang=hi" className="rounded px-2 py-1 hover:bg-muted hover:text-foreground">
            {t(d, 'lang.hi')}
          </Link>
        </nav>
      </header>

      <section
        aria-label="hero"
        data-experiment-variant={heroVariant}
        className="mx-auto flex w-full max-w-3xl flex-col items-center gap-6 px-5 pb-16 pt-12 text-center sm:pt-20"
      >
        <h1 className="text-4xl font-extrabold leading-tight tracking-tight text-foreground sm:text-5xl">
          {t(d, 'hero.title')}
        </h1>
        <p className="max-w-2xl text-lg leading-relaxed text-muted-foreground">{t(d, 'hero.subtitle')}</p>
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
          <Link
            href="/team/signup"
            data-testid="hero-cta"
            className="rounded-xl bg-primary px-8 py-3 text-base font-semibold text-primary-foreground shadow-sm transition hover:bg-primary/90"
          >
            {t(d, 'hero.cta')}
          </Link>
        )}
        <FoundingCounterWidget initial={initial} />
      </section>

      <section aria-label="value" className="border-t border-border bg-muted/40 px-5 py-16">
        <div className="mx-auto w-full max-w-5xl">
          <h2 className="text-center text-2xl font-bold tracking-tight text-foreground sm:text-3xl">
            {t(d, 'value.title')}
          </h2>
          <div className="mt-10 grid gap-6 sm:grid-cols-3">
            {VALUE_CARDS.map((n) => (
              <article
                key={n}
                className="rounded-2xl border border-border bg-card p-6 shadow-sm"
              >
                <h3 className="text-lg font-semibold text-foreground">{t(d, `value.${n}.title`)}</h3>
                <p className="mt-2 leading-relaxed text-muted-foreground">{t(d, `value.${n}.body`)}</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <SocialProof
        data={socialProofData as SocialProofData}
        labels={{
          heading: t(d, 'social.heading'),
          testimonials_placeholder: t(d, 'social.testimonials_placeholder'),
          metrics_placeholder: t(d, 'social.metrics_placeholder'),
        }}
      />

      <section aria-label="pricing" className="border-t border-border bg-muted/40 px-5 py-16">
        <div className="mx-auto w-full max-w-4xl">
          <h2 className="text-center text-2xl font-bold tracking-tight text-foreground sm:text-3xl">
            {t(d, 'pricing.title')}
          </h2>
          <div className="mt-10 grid gap-6 sm:grid-cols-2">
            {prices.map((p) => (
              <article
                key={p.tier}
                data-tier={p.tier}
                className={
                  p.tier === 'founding'
                    ? 'relative flex flex-col rounded-2xl border-2 border-primary bg-card p-7 shadow-md'
                    : 'flex flex-col rounded-2xl border border-border bg-card p-7 shadow-sm'
                }
              >
                <h3 className="text-xl font-bold text-foreground">
                  {t(d, `pricing.${p.tier}.name`)}
                </h3>
                <p className="mt-1 text-sm text-muted-foreground">{t(d, `pricing.${p.tier}.tagline`)}</p>
                <p className="mt-4">
                  <span
                    data-testid={`price-${p.tier}`}
                    className="text-4xl font-extrabold tracking-tight text-foreground"
                  >
                    ₹{p.inr}
                  </span>{' '}
                  <span className="text-sm text-muted-foreground">{t(d, 'pricing.period')}</span>
                </p>
                <ul className="mt-5 flex-1 space-y-2 text-sm text-foreground">
                  {FEATURES.map((f) => (
                    <li key={f} className="flex gap-2">
                      <span aria-hidden className="font-bold text-primary">
                        ✓
                      </span>
                      {t(d, `pricing.feature.${f}`)}
                    </li>
                  ))}
                </ul>
                <Link
                  href={`/team/signup?plan=${p.tier}`}
                  className={
                    p.tier === 'founding'
                      ? 'mt-6 rounded-xl bg-primary px-5 py-2.5 text-center font-semibold text-primary-foreground transition hover:bg-primary/90'
                      : 'mt-6 rounded-xl border border-input px-5 py-2.5 text-center font-semibold text-foreground transition hover:bg-muted'
                  }
                >
                  {t(d, 'pricing.cta')}
                </Link>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section aria-label="day39" className="px-5 py-16">
        <div className="mx-auto w-full max-w-3xl rounded-2xl bg-secondary px-7 py-10 text-center text-secondary-foreground">
          <h2 className="text-2xl font-bold tracking-tight">{t(d, 'day39.title')}</h2>
          <p className="mt-3 leading-relaxed text-secondary-foreground/80">{t(d, 'day39.body')}</p>
        </div>
      </section>

      <section aria-label="faq" className="border-t border-border px-5 py-16">
        <div className="mx-auto w-full max-w-3xl">
          <h2 className="text-center text-2xl font-bold tracking-tight text-foreground sm:text-3xl">
            {t(d, 'faq.title')}
          </h2>
          <div className="mt-8 divide-y divide-border rounded-2xl border border-border bg-card">
            {FAQS.map((n) => (
              <details key={n} className="group px-5 py-4">
                <summary className="cursor-pointer list-none font-medium text-foreground transition group-open:text-primary">
                  {t(d, `faq.q${n}`)}
                </summary>
                <p className="mt-2 leading-relaxed text-muted-foreground">{t(d, `faq.a${n}`)}</p>
              </details>
            ))}
          </div>
        </div>
      </section>

      <footer className="border-t border-border bg-muted/40 px-5 py-10">
        <div className="mx-auto flex w-full max-w-5xl flex-col items-center gap-4 text-center">
          <p className="font-medium text-foreground">{t(d, 'footer.tagline')}</p>
          <nav aria-label="legal" className="flex flex-wrap justify-center gap-x-2 text-sm text-muted-foreground">
            {/* NEEDS-FAZAL: the legal pages themselves are a follow-up (not live pre-launch). */}
            <Link href="/team/privacy" className="hover:text-foreground hover:underline">
              {t(d, 'footer.privacy')}
            </Link>
            <span aria-hidden>·</span>
            <Link href="/team/dpdp" className="hover:text-foreground hover:underline">
              {t(d, 'footer.dpdpa')}
            </Link>
            <span aria-hidden>·</span>
            <Link href="/team/terms" className="hover:text-foreground hover:underline">
              {t(d, 'footer.terms')}
            </Link>
            <span aria-hidden>·</span>
            <Link href="/team/contact" className="hover:text-foreground hover:underline">
              {t(d, 'footer.contact')}
            </Link>
          </nav>
          <small className="text-xs text-muted-foreground">{t(d, 'footer.rights')}</small>
        </div>
      </footer>
    </main>
  )
}
