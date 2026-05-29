/**
 * VT-203 — minimal Ops Console login page (magic-link).
 *
 * Owner enters email; POST to /api/ops/login triggers Supabase Auth
 * signInWithOtp(); callback at /api/ops/login/callback verifies the
 * Supabase session + mints the operator JWT cookie scoped to /team/ops.
 *
 * No styling beyond Tailwind defaults. UI polish = separate row.
 */

export const dynamic = 'force-dynamic'

export default function OpsLoginPage({
  searchParams,
}: {
  searchParams?: { sent?: string; error?: string }
}) {
  const sent = searchParams?.sent === '1'
  const errorParam = searchParams?.error

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
          <button type="submit">Email magic link</button>
        </form>
      )}

      {errorParam ? (
        <p data-state="error">Sign-in error: {errorParam}</p>
      ) : null}
    </main>
  )
}
