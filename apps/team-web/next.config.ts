import type { NextConfig } from 'next'

const nextConfig: NextConfig = {
  // VT-508 — bake deploy-identity into the bundle at build time so the DeployStamp component
  // can render `web <sha7> · HH:MM` without a runtime env lookup. VERCEL_GIT_COMMIT_SHA is
  // injected automatically by Vercel in the build environment; GIT_COMMIT_SHA is the fallback
  // for local dev / CI. NEXT_PUBLIC_* vars are embedded in the client bundle by Next.js.
  env: {
    NEXT_PUBLIC_BUILD_SHA:
      process.env.VERCEL_GIT_COMMIT_SHA ?? process.env.GIT_COMMIT_SHA ?? 'dev',
    NEXT_PUBLIC_BUILD_TIME: new Date().toISOString(),
  },
}

export default nextConfig
