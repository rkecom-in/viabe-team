'use client'

/**
 * VT-517 — the signup OWNERSHIP step: an honest "pending Viabe team review" screen
 * (bilingual EN/HI).
 *
 * VT-517 KILLED all self-serve ownership OTP/DIN: a GST-registered entity being real is
 * NOT proof the signer OWNS it, and an automated channel-OTP can't establish that — so
 * ownership is decided by a Viabe human (the VTR Ops Console ownership-review surface).
 * This screen tells the owner the truth: the account is set up, but the AI agent will
 * NOT act on their customers until Viabe verifies ownership. Continue advances the
 * wizard to the dashboard; EXECUTION stays gated SERVER-SIDE until a VTR marks ownership
 * verified — we NEVER claim the owner is "verified" here.
 *
 * 2026-08-21 — redesigned to the Claude Design prototype's step 4. The screen now leads
 * with what is still switched OFF rather than with the celebration, which is the honest
 * ordering: the account existing is the smaller fact. No network call, and the four e2e
 * harness hooks (`data-ownership-step`, `data-tenant-id`, `data-ownership-business`,
 * `data-ownership-continue`) are unchanged.
 */

import type { Lang } from './signup-copy'
import { t } from './signup-copy'
import * as ui from '@/lib/viabe-ui'

/** The onboarding wizard's actual steps (VT-267 PR-C), not the prototype's invented four. */
const TODOS = [
  { title: 'td1', note: 'td1n' },
  { title: 'td2', note: 'td2n' },
  { title: 'td3', note: 'td3n' },
] as const

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
  return (
    <section
      data-ownership-step="pending"
      data-tenant-id={tenantId}
      className="flex flex-col gap-4"
    >
      {/* The gate first. An owner who reads only one block should read this one. */}
      <div className={ui.alertWarn} role="status">
        <span className={ui.alertTitle}>{t(lang, 'bannerTitle')}</span>
        <p className={ui.alertBody}>{t(lang, 'bannerBody')}</p>
        <span className="text-xs leading-[1.55] opacity-90">{t(lang, 'reviewMeta')}</span>
      </div>

      <div className={`${ui.panel} flex flex-col gap-5`}>
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2.5">
            <span className={ui.successDot} aria-hidden>
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="3.4"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M20 6 9 17l-5-5" />
              </svg>
            </span>
            <h2 className={ui.h2}>{t(lang, 's4title')}</h2>
          </div>
          {businessName && (
            <p data-ownership-business className={`${ui.monoValue} text-base`}>
              {businessName}
            </p>
          )}
          <p className={ui.body}>{t(lang, 's4sub')}</p>
        </div>

        <ol className="m-0 flex list-none flex-col gap-2.5 p-0">
          {TODOS.map((item, i) => (
            <li
              key={item.title}
              className="flex items-start gap-3 rounded-xl border border-border bg-background p-3.5"
            >
              <span
                className="flex h-6 w-6 flex-none items-center justify-center rounded-full border border-border bg-card font-display text-xs font-bold text-muted-foreground"
                aria-hidden
              >
                {i + 1}
              </span>
              <span className="flex flex-col gap-0.5">
                <span className="font-display text-sm font-bold text-foreground">
                  {t(lang, item.title)}
                </span>
                <span className="text-xs leading-[1.55] text-muted-foreground">
                  {t(lang, item.note)}
                </span>
              </span>
            </li>
          ))}
        </ol>

        <div className="flex flex-col gap-2">
          <button
            type="button"
            data-ownership-continue
            onClick={onVerified}
            className={ui.primaryButton()}
          >
            {t(lang, 'signInCta')}
          </button>
          <p className={`${ui.hint} text-center`}>{t(lang, 'signInNote')}</p>
        </div>
      </div>

      <p className={`${ui.hint} text-center`}>{t(lang, 'footer')}</p>
    </section>
  )
}
