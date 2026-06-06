import type { Metadata } from 'next'

import { LegalShell } from '../legal-shell'

// VT-353: noindex-gated until Fazal approves the binding copy (DRAFT shell).
export const metadata: Metadata = { robots: { index: false, follow: false } }
export const dynamic = 'force-dynamic'

export default async function PrivacyPage({
  searchParams,
}: {
  searchParams: Promise<{ lang?: string }>
}) {
  const { lang } = await searchParams
  return <LegalShell pageKey="privacy" lang={lang} />
}
