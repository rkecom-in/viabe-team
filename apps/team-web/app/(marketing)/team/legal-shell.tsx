import Link from 'next/link'

import { getLegalDictionary, resolveLocale, t, type Locale } from '@/lib/i18n'

/**
 * VT-353 — shared shell for the 4 public legal pages (privacy / dpdp / terms / contact).
 *
 * These are bilingual (EN + HI) DRAFT SHELLS: they exist so the VT-95 landing + VT-96 signup
 * footer links resolve (was 404 — itself a DPDP problem for a public signup) and are banner-marked
 * "DRAFT — pending counsel review" + noindex-gated (per page). The BINDING legal copy is
 * NEEDS-FAZAL / counsel-authored (CLAUDE.md: CC does not draft binding legal text) — it lands here
 * once Fazal approves it. Locale = `?lang=en|hi` (the landing pattern).
 */
export type LegalPageKey = 'privacy' | 'dpdp' | 'terms' | 'contact'

export function LegalShell({ pageKey, lang }: { pageKey: LegalPageKey; lang?: string }) {
  const locale: Locale = resolveLocale(lang)
  const d = getLegalDictionary(locale)
  return (
    <main
      lang={locale}
      style={{ maxWidth: 720, margin: '0 auto', padding: '2rem 1.25rem', lineHeight: 1.6 }}
    >
      <Link href={`/team?lang=${locale}`} style={{ fontSize: 14 }}>
        {t(d, 'nav.home')}
      </Link>
      <div
        role="alert"
        style={{
          margin: '1rem 0',
          padding: '0.75rem 1rem',
          border: '1px solid hsl(var(--gold))',
          background: 'hsl(var(--gold) / 0.12)',
          color: 'hsl(var(--gold-foreground))',
          borderRadius: 8,
          fontSize: 14,
        }}
      >
        ⚠️ {t(d, 'draft.banner')}
      </div>
      <h1>{t(d, `${pageKey}.title`)}</h1>
      <p style={{ color: 'hsl(var(--muted-foreground))' }}>{t(d, `${pageKey}.intro`)}</p>
      <p>{t(d, `${pageKey}.body`)}</p>
      <p style={{ marginTop: '1.5rem', fontSize: 13, color: 'hsl(var(--muted-foreground))' }}>
        {t(d, 'updated.label')}: {t(d, 'updated.value')}
      </p>
    </main>
  )
}
