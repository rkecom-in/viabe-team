import { fileURLToPath } from 'node:url'

import { defineConfig } from 'vitest/config'

// `@/` resolves to the app root, matching tsconfig paths.
export default defineConfig({
  resolve: {
    alias: { '@': fileURLToPath(new URL('.', import.meta.url)) },
  },
  test: {
    include: ['tests/**/*.test.ts'],
    environment: 'node',
  },
})
