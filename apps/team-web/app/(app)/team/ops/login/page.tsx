/**
 * VT-203 — minimal Ops Console login page (magic-link).
 * VT-230 — accept `?next=<path>` and forward to POST as hidden field.
 *
 * Owner enters email; POST to /api/ops/login triggers Supabase Auth
 * signInWithOtp(); callback at /api/ops/login/callback verifies the
 * Supabase session + mints the operator JWT cookie scoped to /team.
 *
 * No styling beyond Tailwind defaults. UI polish = separate row.
 */

export const dynamic = 'force-dynamic'

export default function OpsLoginPage({
  searchParams,
}: {
  searchParams?: { sent?: string; error?: string; next?: string }
}) {
  const sent = searchParams?.sent === '1'
  const errorParam = searchParams?.error
  // VT-230: forward ?next= through to the POST handler so the email
  // magic-link callback can land the operator on the requested page.
  // Validated against an allowlist at the callback (apps/team-web/app/api/ops/login/callback/route.ts).
  const nextParam = searchParams?.next ?? ''

  return (
    <main className="ops-login" data-area="team-ops-login">
      <header>
        <h1>Ops Console — Sign in</h1>
      </header>

      {sent ? (
        <p data-state="sent">Check your email for the sign-in link.</p>
      ) : (
        <form action="/api/ops/login" method="post">
          <label htmlFor="email">Operator email</label>
          <input
            id="email"
            name="email"
            type="email"
            required
            autoComplete="email"
          />
          {nextParam ? (
            <input type="hidden" name="next" value={nextParam} />
          ) : null}
          <button type="submit">Email magic link</button>
        </form>
      )}

      {errorParam ? (
        <p data-state="error">Sign-in error: {errorParam}</p>
      ) : null}
    </main>
  )
}
