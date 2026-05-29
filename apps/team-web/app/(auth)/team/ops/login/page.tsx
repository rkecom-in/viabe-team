/**
 * VT-203 — Ops Console login (magic-link).
 * VT-230 — accept ?next= forward as hidden field.
 * VT-232 — lifted from (app)/team/ops/login/ to (auth)/team/ops/login/
 *          so OpsLayout's banner doesn't render on top of the form.
 *          Tailwind utilities added for usable presentation.
 *
 * Auth surface lives in a SIBLING route group `(auth)/`. Next.js does
 * NOT apply `(app)/team/ops/layout.tsx` here — no banner, no auth gate,
 * just the form.
 */

export const dynamic = 'force-dynamic'

export default function OpsLoginPage({
  searchParams,
}: {
  searchParams?: { sent?: string; error?: string; next?: string }
}) {
  const sent = searchParams?.sent === '1'
  const errorParam = searchParams?.error
  const nextParam = searchParams?.next ?? ''

  return (
    <main
      className="min-h-screen flex items-center justify-center bg-gray-50 p-4"
      data-area="team-ops-login"
    >
      <div className="w-full max-w-md bg-white shadow-md rounded-lg p-8 space-y-6">
        <header className="text-center">
          <h1 className="text-2xl font-semibold text-gray-900">
            Ops Console
          </h1>
          <p className="text-sm text-gray-600 mt-2">Sign in to continue</p>
        </header>

        {sent ? (
          <p
            data-state="sent"
            className="text-center text-sm text-green-700 bg-green-50 border border-green-200 rounded p-3"
          >
            Check your email for the sign-in link.
          </p>
        ) : (
          <form action="/api/ops/login" method="post" className="space-y-4">
            <div>
              <label
                htmlFor="email"
                className="block text-sm font-medium text-gray-700 mb-1"
              >
                Operator email
              </label>
              <input
                id="email"
                name="email"
                type="email"
                required
                autoComplete="email"
                className="w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
            </div>
            <div>
              <label
                htmlFor="password"
                className="block text-sm font-medium text-gray-700 mb-1"
              >
                Password
              </label>
              <input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                className="w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
              <p className="text-xs text-gray-500 mt-1">
                Leave blank to receive a magic link instead.
              </p>
            </div>
            {nextParam ? (
              <input type="hidden" name="next" value={nextParam} />
            ) : null}
            <button
              type="submit"
              data-element="signin-button"
              className="w-full bg-blue-600 hover:bg-blue-700 text-white font-medium py-2 px-4 rounded-md shadow-sm transition-colors"
            >
              Sign in
            </button>
          </form>
        )}

        {errorParam ? (
          <p
            data-state="error"
            className="text-sm text-red-700 bg-red-50 border border-red-200 rounded p-3"
          >
            Sign-in error: {errorParam}
          </p>
        ) : null}
      </div>
    </main>
  )
}
