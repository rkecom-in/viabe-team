'use client'

/**
 * VT-517 — the signup OWNERSHIP step, now an honest "pending Viabe team review" screen (bilingual
 * EN/HI).
 *
 * VT-517 KILLED all self-serve ownership OTP/DIN: a GST-registered entity being real is NOT proof the
 * signer OWNS it, and an automated channel-OTP can't establish that — so ownership is now decided by a
 * Viabe human (the VTR Ops Console ownership-review surface). This screen tells the owner the truth:
 * the account is set up, but the AI agent will NOT act on their customers until Viabe verifies
 * ownership (a quick manual review). Continue advances the wizard to the dashboard; EXECUTION stays
 * gated SERVER-SIDE until a VTR marks ownership verified — we NEVER claim the owner is "verified" here.
 *
 * The call site is unchanged (tenantId, businessName, onVerified) + lang for the bilingual copy. No
 * network call — Continue just closes the wizard (onVerified). tenantId is surfaced as a data attribute
 * for the e2e harness.
 */

type Lang = 'en' | 'hi'

type OwMsgKey = 'heading' | 'body' | 'reassure' | 'continue'

const OW_MESSAGES: Record<Lang, Record<OwMsgKey, string>> = {
  en: {
    heading: 'Your account is set up',
    body: 'Before our AI agent starts working with your customers, the Viabe team does a quick manual check to confirm you own this business.',
    reassure:
      'You can continue to your dashboard now. Your agent starts acting for you once that review is done — we’ll let you know on WhatsApp.',
    continue: 'Continue to dashboard',
  },
  hi: {
    heading: 'आपका खाता तैयार है',
    body: 'हमारा AI एजेंट आपके ग्राहकों के साथ काम शुरू करे, उससे पहले Viabe टीम एक त्वरित मैन्युअल जांच करती है कि यह व्यवसाय आपका है।',
    reassure:
      'आप अभी अपने डैशबोर्ड पर जा सकते हैं। वह जांच पूरी होते ही आपका एजेंट आपके लिए काम करना शुरू कर देगा — हम आपको WhatsApp पर बता देंगे।',
    continue: 'डैशबोर्ड पर जाएं',
  },
}

export function OwnershipStep({
  tenantId,
  businessName,
  lang,
  onVerified,
}: {
  /** The REAL tenant_id (from the create 201). Surfaced as a data attribute; no network call here. */
  tenantId: string
  businessName: string
  lang: Lang
  /** Called on Continue — closes the wizard so the owner reaches the dashboard. EXECUTION stays gated
   *  server-side until a VTR verifies ownership; this is NOT an "ownership verified" signal. */
  onVerified: () => void
}) {
  const t = OW_MESSAGES[lang]
  const card = 'rounded-2xl border border-border bg-card p-6 shadow-sm sm:p-8'
  const primaryBtn =
    'rounded-xl bg-primary px-5 py-3 font-semibold text-primary-foreground shadow-sm transition hover:bg-primary/90'

  return (
    <section data-ownership-step="pending" data-tenant-id={tenantId} className={`mt-8 ${card}`}>
      <div className="flex items-center gap-3">
        {/* VT-511 design language — celebratory account-ready header (NOT an ownership-verified claim). */}
        <span
          aria-hidden
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-secondary text-xl font-bold text-secondary-foreground"
        >
          ✓
        </span>
        <h2 className="text-lg font-semibold text-foreground">{t.heading}</h2>
      </div>
      {businessName && (
        <p data-ownership-business className="mt-2 text-base font-semibold text-foreground">
          {businessName}
        </p>
      )}
      <p className="mt-3 text-sm leading-relaxed text-muted-foreground">{t.body}</p>
      <p className="mt-3 text-sm leading-relaxed text-muted-foreground">{t.reassure}</p>
      <button type="button" data-ownership-continue onClick={onVerified} className={`mt-5 w-full ${primaryBtn}`}>
        {t.continue}
      </button>
    </section>
  )
}
