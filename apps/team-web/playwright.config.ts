import { defineConfig, devices } from '@playwright/test'

/**
 * VT-123 Playwright config.
 *
 * Tests assume an already-running team-web dev server at
 * `http://localhost:3000` (no managed `webServer` block; the Python
 * canary is the orchestration script that boots team-web + supplies
 * test data).
 */
export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  fullyParallel: false,
  reporter: [['list']],
  use: {
    baseURL: process.env.TEAM_WEB_BASE_URL ?? 'http://localhost:3000',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
