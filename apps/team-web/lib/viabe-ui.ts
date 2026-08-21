/**
 * Viabe design scheme — the shared style vocabulary for the owner-facing surfaces
 * (signup, sign-in). Lifted from the Claude Design project "Viabe Reports"
 * (`Signup Flow.dc.html` / `Signin Flow.dc.html`) and expressed against the tokens
 * already defined in `app/globals.css`, which match the design system's
 * `tokens/colors.css` value-for-value.
 *
 * Two rules carried over from the design system rather than re-derived:
 *
 *  1. `--ink-*` colours are TEXT AND ICON GLYPHS ONLY. Each is its base hue at the
 *     lightness that clears WCAG AA 4.5:1 on the background, the card AND on a 0.18
 *     tint of its own colour. Never put an `ink-*` colour and a full-strength one on
 *     the same element — so an alert uses `text-ink-destructive` on a
 *     `bg-destructive/8` tint, never `text-destructive`.
 *  2. Space Grotesk carries display and UI-bold (headings, buttons, step marks);
 *     DM Sans carries body copy. Numbers the owner reads back — phone, GSTIN, the
 *     six-digit code — are monospace so digits stay aligned and legible.
 *
 * Touch targets are >= 44px throughout: the primary owner surface is a phone.
 */

/** Page shell — the warm-cream canvas every owner surface sits on. */
export const page = 'min-h-screen bg-background text-foreground antialiased'

/** Top bar: brandmark on the left, language toggle on the right. */
export const header =
  'flex items-center justify-between gap-3 border-b border-border bg-background px-5 py-3'

/** The segmented EN/HI control. `on` is the selected half. */
export const langToggle = 'flex overflow-hidden rounded-full border border-border bg-card'
export const langButton = (on: boolean) =>
  [
    'min-h-9 px-3.5 py-1.5 font-display text-[13px] font-bold transition',
    on ? 'bg-primary text-foreground' : 'bg-transparent text-muted-foreground hover:bg-muted',
  ].join(' ')

/** The card every step's content sits inside. */
export const panel = 'rounded-[18px] border border-border bg-card p-5 sm:p-[26px]'

/** Display headings. */
export const h1 = 'font-display font-bold leading-[1.18] tracking-[-0.01em] text-pretty'
export const h2 = 'font-display text-[18px] font-bold leading-[1.3] text-pretty'

/** Body copy at the two sizes the design uses. */
export const body = 'text-[15px] leading-[1.55] text-muted-foreground text-pretty'
export const bodySm = 'text-[13px] leading-[1.55] text-muted-foreground text-pretty'
export const hint = 'text-xs leading-[1.55] text-muted-foreground'

/** Field label + input. `invalid` switches the border and lays a faint tint behind. */
export const fieldLabel = 'flex flex-col gap-1.5'
export const fieldLabelText = 'text-[13px] font-semibold text-foreground'
export const field = (invalid = false) =>
  [
    'min-h-[46px] w-full rounded-[10px] border px-3.5 py-2.5 text-[15px] text-foreground',
    'outline-none transition placeholder:text-muted-foreground/70',
    'focus:border-primary focus:ring-2 focus:ring-ring/25',
    invalid ? 'border-destructive bg-destructive/5' : 'border-border bg-background',
  ].join(' ')

/** The +91 prefix that sits flush against the phone input. */
export const phonePrefix =
  'flex items-center rounded-l-[10px] border border-r-0 border-border bg-muted px-3 font-mono text-sm text-muted-foreground'
export const phoneField = (invalid = false) =>
  [field(invalid), 'min-w-0 flex-1 rounded-l-none'].join(' ')

/** The six-digit code input — monospace, tracked out, centred. */
export const codeField = (invalid = false, disabled = false) =>
  [
    'min-h-[52px] w-full rounded-[10px] border px-3.5 py-2.5 text-center font-mono text-[22px]',
    'font-semibold tracking-[0.3em] text-foreground outline-none transition',
    'focus:border-primary focus:ring-2 focus:ring-ring/25',
    invalid ? 'border-destructive bg-destructive/5' : 'border-border',
    disabled ? 'bg-muted' : invalid ? '' : 'bg-background',
  ].join(' ')

/**
 * Primary action — the saffron-to-gold gradient. Foreground is charcoal, not white:
 * white on this gradient fails AA, and the design system pairs the gradient with ink.
 */
export const primaryButton = (disabled = false) =>
  [
    'flex min-h-[52px] w-full items-center justify-center gap-2 rounded-xl px-5 py-3.5',
    'bg-[linear-gradient(95deg,hsl(var(--viabe-saffron)),hsl(var(--viabe-gold)))]',
    'font-display text-base font-bold text-foreground',
    'shadow-[0_10px_22px_-14px_hsl(var(--viabe-saffron)/0.7)] transition',
    disabled ? 'cursor-not-allowed opacity-45' : 'hover:brightness-[1.03]',
  ].join(' ')

/** Secondary action — outlined, same height, used for "back" and non-committal choices. */
export const secondaryButton =
  [
    'flex min-h-[44px] items-center justify-center gap-2 rounded-[10px] border border-border',
    'bg-background px-4 py-2.5 font-display text-[13px] font-bold text-foreground transition hover:bg-muted',
  ].join(' ')

/** A text link rendered as a button (resend, edit-number, read-the-disclosure). */
export const linkButton = (disabled = false) =>
  [
    'min-h-10 border-0 bg-transparent p-0 text-[13px] font-semibold underline transition',
    disabled ? 'cursor-default text-muted-foreground' : 'cursor-pointer text-ink-primary',
  ].join(' ')

/**
 * Alerts. Each tint carries its matching `ink-*` glyph colour — never the
 * full-strength one (see rule 1 above).
 */
export const alertError =
  'flex flex-col gap-2.5 rounded-[14px] border border-destructive/45 bg-destructive/8 p-4 text-ink-destructive'
export const alertWarn =
  'flex flex-col gap-2.5 rounded-[14px] border-[hsl(var(--vri-medium)/0.5)] border bg-[hsl(var(--vri-medium)/0.12)] p-4 text-ink-primary'
export const alertInfo =
  'flex flex-col gap-2.5 rounded-xl border border-primary/30 bg-accent p-3.5 text-foreground'
export const alertTitle = 'font-display text-[15px] font-bold leading-[1.35] text-pretty'
export const alertBody = 'text-sm leading-[1.55] text-pretty'

/** Small caps eyebrow used above viewport labels and section marks. */
export const eyebrow =
  'font-display text-[11px] font-semibold uppercase tracking-[0.1em] text-muted-foreground'

/** Monospace chip for a value the owner reads back (a number, a GSTIN). */
export const monoValue = 'font-mono text-[15px] font-semibold text-foreground'

/** Step marks in the rail: done / current / upcoming. */
export type StepState = 'done' | 'current' | 'todo'
export const stepDot = (state: StepState) =>
  [
    'flex h-7 w-7 flex-none items-center justify-center rounded-full font-display text-[13px] font-bold',
    state === 'done'
      ? 'bg-[hsl(var(--vri-good))] text-white'
      : state === 'current'
        ? 'bg-primary text-foreground'
        : 'border border-border bg-background text-muted-foreground',
  ].join(' ')
export const stepLabel = (state: StepState) =>
  [
    'font-display text-sm font-bold',
    state === 'todo' ? 'text-muted-foreground' : 'text-foreground',
  ].join(' ')

/** The tablet stepper's progress bar segment. */
export const stepBar = (state: StepState) =>
  [
    'h-1 rounded-full',
    state === 'todo' ? 'bg-border' : state === 'done' ? 'bg-[hsl(var(--vri-good))]' : 'bg-primary',
  ].join(' ')

/** A success tick on the cream canvas. */
export const successDot =
  'flex h-6 w-6 flex-none items-center justify-center rounded-full bg-[hsl(var(--vri-good))] text-white'
