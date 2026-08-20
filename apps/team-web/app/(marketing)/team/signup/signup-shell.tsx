'use client'

/**
 * The shared chrome for the signup wizard, from the Claude Design prototype
 * (`Signup Flow.dc.html`): a brandmark + language header, and a step indicator that
 * takes three different shapes by width —
 *
 *   desktop (>=1024)  a sticky left rail: heading, four labelled steps, a closing note
 *   tablet  (>=640)   a horizontal four-segment bar above the panel
 *   mobile  (<640)    "Step n of 4", what's next, and a thin progress bar
 *
 * The breakpoints are Tailwind's, so the three render from one tree with no
 * ResizeObserver and no layout-shift on hydration (the prototype measures its own
 * width because a canvas artboard has no viewport; a real page does).
 */

import Image from 'next/image'
import type { CopyKey, Lang } from './signup-copy'
import { t } from './signup-copy'
import * as ui from '@/lib/viabe-ui'

export type WizardStep = 'details' | 'entity' | 'verify' | 'ownership'

/** One row per step: the wizard key, and the two copy keys that describe it. */
const STEPS = [
  { key: 'details', title: 's1', note: 's1note' },
  { key: 'entity', title: 's2', note: 's2note' },
  { key: 'verify', title: 's3', note: 's3note' },
  { key: 'ownership', title: 's4', note: 's4note' },
] as const satisfies ReadonlyArray<{ key: WizardStep; title: CopyKey; note: CopyKey }>

export function stepIndex(step: WizardStep): number {
  return STEPS.findIndex((s) => s.key === step)
}

function stateOf(index: number, current: number): ui.StepState {
  if (index < current) return 'done'
  if (index === current) return 'current'
  return 'todo'
}

/** Brandmark left, EN/HI toggle right. Present on every step. */
export function SignupHeader({
  lang,
  onLang,
}: {
  lang: Lang
  onLang: (l: Lang) => void
}) {
  return (
    <header className={ui.header}>
      <Image
        src="/brand/header-logo-light.webp"
        alt="Viabe"
        width={173}
        height={34}
        priority
        className="h-[34px] w-auto"
      />
      <div className="flex items-center gap-2">
        <span className="text-xs text-muted-foreground">{t(lang, 'langLabel')}</span>
        <div className={ui.langToggle}>
          <button
            type="button"
            onClick={() => onLang('en')}
            aria-pressed={lang === 'en'}
            className={ui.langButton(lang === 'en')}
          >
            English
          </button>
          <button
            type="button"
            onClick={() => onLang('hi')}
            aria-pressed={lang === 'hi'}
            className={ui.langButton(lang === 'hi')}
          >
            हिन्दी
          </button>
        </div>
      </div>
    </header>
  )
}

/** Desktop only — the sticky rail with the four steps spelled out. */
export function StepRail({ lang, step }: { lang: Lang; step: WizardStep }) {
  const current = stepIndex(step)
  return (
    <aside className="sticky top-7 hidden flex-col gap-6 lg:flex">
      <div>
        <h1 className={`${ui.h1} text-[30px]`}>{t(lang, 'h1')}</h1>
        <p className={`${ui.body} mt-3`}>{t(lang, 'sub')}</p>
      </div>
      <ol className="m-0 flex list-none flex-col gap-0.5 p-0">
        {STEPS.map((s, i) => {
          const state = stateOf(i, current)
          return (
            <li key={s.key} className="flex items-start gap-3 py-2.5">
              <span className={ui.stepDot(state)} aria-hidden>
                {state === 'done' ? '✓' : i + 1}
              </span>
              <span className="flex flex-col gap-0.5 pt-0.5">
                <span className={ui.stepLabel(state)}>{t(lang, s.title)}</span>
                <span className="text-xs text-muted-foreground">{t(lang, s.note)}</span>
              </span>
            </li>
          )
        })}
      </ol>
      <div className={`${ui.bodySm} border-t border-border pt-4.5`}>{t(lang, 'railNote')}</div>
    </aside>
  )
}

/** Tablet — heading plus a four-segment bar. Hidden on mobile and desktop. */
export function StepStrip({ lang, step }: { lang: Lang; step: WizardStep }) {
  const current = stepIndex(step)
  return (
    <div className="hidden flex-col gap-4.5 sm:flex lg:hidden">
      <div>
        <h1 className={`${ui.h1} text-[26px]`}>{t(lang, 'h1')}</h1>
        <p className={`${ui.body} mt-2`}>{t(lang, 'sub')}</p>
      </div>
      <ol className="m-0 grid list-none grid-cols-4 gap-2.5 p-0">
        {STEPS.map((s, i) => {
          const state = stateOf(i, current)
          return (
            <li key={s.key} className="flex flex-col gap-2">
              <span className={ui.stepBar(state)} aria-hidden />
              <span className="flex items-center gap-1.5">
                <span
                  className={`${ui.stepDot(state)} h-5 w-5 text-[11px]`}
                  aria-hidden
                >
                  {state === 'done' ? '✓' : i + 1}
                </span>
                <span className={`${ui.stepLabel(state)} text-xs`}>{t(lang, s.title)}</span>
              </span>
            </li>
          )
        })}
      </ol>
    </div>
  )
}

/** Mobile — position, what's next, a progress bar, and the current step's title. */
export function StepProgress({ lang, step }: { lang: Lang; step: WizardStep }) {
  const current = stepIndex(step)
  const nextStep = STEPS[current + 1]
  const next = nextStep ? t(lang, nextStep.title) : null
  return (
    <div className="flex flex-col gap-1.5 sm:hidden">
      <div className="flex items-center justify-between gap-2.5">
        <span className="font-mono text-xs font-semibold tracking-[0.04em] text-ink-primary">
          {t(lang, 'stepOf', { n: String(current + 1) })}
        </span>
        {next && (
          <span className="text-xs text-muted-foreground">{t(lang, 'nextUp', { title: next })}</span>
        )}
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-border" aria-hidden>
        <div
          className="h-full rounded-full bg-[linear-gradient(95deg,hsl(var(--viabe-saffron)),hsl(var(--viabe-gold)))] transition-[width] duration-300"
          style={{ width: `${((current + 1) / 4) * 100}%` }}
        />
      </div>
      <h1 className={`${ui.h1} mt-1.5 text-[22px]`}>{t(lang, STEPS[current]?.title ?? 's1')}</h1>
    </div>
  )
}

/** The two-column frame: rail on the left at desktop, panel column on the right. */
export function SignupLayout({
  lang,
  onLang,
  step,
  children,
}: {
  lang: Lang
  onLang: (l: Lang) => void
  step: WizardStep
  children: React.ReactNode
}) {
  return (
    <div className={`${ui.page} flex flex-col`}>
      <SignupHeader lang={lang} onLang={onLang} />
      <main className="flex flex-1 justify-center px-4 py-6 sm:px-8 sm:py-12">
        <div className="grid w-full max-w-[1080px] gap-10 lg:grid-cols-[320px_minmax(0,1fr)]">
          <StepRail lang={lang} step={step} />
          <div className="flex min-w-0 flex-col gap-4">
            <StepProgress lang={lang} step={step} />
            <StepStrip lang={lang} step={step} />
            {children}
          </div>
        </div>
      </main>
    </div>
  )
}
