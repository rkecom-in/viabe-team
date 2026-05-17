// Flat ESLint config. Next.js 16 removed the `next lint` command, so linting
// runs via the ESLint CLI (`eslint .`). eslint-config-next 16 ships flat
// config natively — `core-web-vitals` is the array we spread in.
import nextCoreWebVitals from 'eslint-config-next/core-web-vitals'

/** @type {import('eslint').Linter.Config[]} */
const config = [
  ...nextCoreWebVitals,
  {
    ignores: ['.next/**', 'next-env.d.ts'],
  },
]

export default config
