/**
 * VT-201 PR-3 — Playwright JWT-mint fixture.
 *
 * Mints a real operator JWT inline using the same `jose` HS256 helper
 * (`lib/auth/operator-jwt.ts::issueOperatorJwt`) that team-web's
 * `requireFazal()` verifies. Tests using this fixture pass the auth
 * gate without depending on a pre-provisioned cookie.
 *
 * Requires `OPERATOR_JWT_SECRET` + `FAZAL_OWNER_UUID` env vars to be
 * present (CI: provisioned by the e2e-playwright job; local: source
 * `.viabe/secrets/supabase-dev.env` before running playwright).
 */

import { test as base } from '@playwright/test'
import { SignJWT } from 'jose'

const OPERATOR_TOKEN_TTL_SEC = 60 * 60  // 1 hour for test runs

async function mintOperatorJwt(secret: string, operatorId: string): Promise<string> {
  return await new SignJWT({ operator_id: operatorId, operator_claim: true })
    .setProtectedHeader({ alg: 'HS256' })
    .setSubject(operatorId)
    .setAudience('authenticated')
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.now() / 1000) + OPERATOR_TOKEN_TTL_SEC)
    .sign(new TextEncoder().encode(secret))
}

export interface FazalAuthFixture {
  fazalJwt: string
}

export const test = base.extend<FazalAuthFixture>({
  fazalJwt: async ({ context }, provide) => {
    const secret = process.env.OPERATOR_JWT_SECRET ?? ''
    const fazalUuid = process.env.FAZAL_OWNER_UUID ?? ''
    if (!secret || !fazalUuid) {
      throw new Error(
        'fazalJwt fixture: OPERATOR_JWT_SECRET + FAZAL_OWNER_UUID required',
      )
    }
    const jwt = await mintOperatorJwt(secret, fazalUuid)
    await context.addCookies([
      {
        name: 'viabe_ops_jwt',
        value: jwt,
        domain: 'localhost',
        path: '/',
        httpOnly: true,
        secure: false,
        sameSite: 'Lax',
      },
    ])
    await provide(jwt)
  },
})

export { expect } from '@playwright/test'
