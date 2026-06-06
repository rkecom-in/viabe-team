/**
 * VT-338 — minimal i18n for the owner portal (EN + HI). No runtime dep: locale dicts are
 * JSON, resolved server-side. The active locale = an explicit ?lang override > the tenant
 * default (tenant.preferred_language) > 'en'. Never throws.
 *
 * NEEDS-FAZAL: the Hindi copy (locales/hi.json) is a first pass — Fazal reviews the wording.
 */
import landingEn from '@/locales/team-landing/en.json'
import landingHi from '@/locales/team-landing/hi.json'
import legalEn from '@/locales/team-legal/en.json'
import legalHi from '@/locales/team-legal/hi.json'
import en from '@/locales/en.json'
import hi from '@/locales/hi.json'

export type Locale = 'en' | 'hi'

const DICTS: Record<Locale, Record<string, string>> = { en, hi }
// VT-95: the public landing copy is a SEPARATE namespace from the dashboard dict, so the two
// surfaces evolve independently. Same loader shape (getLandingDictionary + t).
const LANDING_DICTS: Record<Locale, Record<string, string>> = { en: landingEn, hi: landingHi }
// VT-353: the public legal pages (privacy / dpdp / terms / contact) — a SEPARATE namespace again
// (these are DRAFT shells; the binding copy is NEEDS-FAZAL / counsel-authored).
const LEGAL_DICTS: Record<Locale, Record<string, string>> = { en: legalEn, hi: legalHi }

function _pick(v?: string | null): Locale | null {
  return v === 'hi' || v === 'en' ? v : null
}

export function resolveLocale(override?: string | null, tenantDefault?: string | null): Locale {
  return _pick(override) ?? _pick(tenantDefault) ?? 'en'
}

export function getDictionary(locale: Locale): Record<string, string> {
  return DICTS[locale] ?? DICTS.en
}

/** VT-95: the public landing-page dictionary (locales/team-landing/{en,hi}.json). */
export function getLandingDictionary(locale: Locale): Record<string, string> {
  return LANDING_DICTS[locale] ?? LANDING_DICTS.en
}

/** VT-353: the public legal-pages dictionary (locales/team-legal/{en,hi}.json). */
export function getLegalDictionary(locale: Locale): Record<string, string> {
  return LEGAL_DICTS[locale] ?? LEGAL_DICTS.en
}

/** Look up a key; falls back to the key itself if missing (never throws). */
export function t(dict: Record<string, string>, key: string): string {
  return dict[key] ?? key
}
