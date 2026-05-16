import { defineConfig } from 'vitest/config'

// Root-level test run. Covers repo tooling (the cross-product env lint rule).
// App/package tests live in their own workspaces.
export default defineConfig({
  test: {
    include: ['scripts/**/*.test.mjs'],
  },
})
